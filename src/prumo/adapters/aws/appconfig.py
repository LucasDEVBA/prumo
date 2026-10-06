"""Versão do prompt em produção guardada no AWS AppConfig (configuração hospedada).

O conteúdo hospedado é `{"active": "v2", "history": ["v1", "v2"]}`: `history` termina na versão
ativa, e o rollback volta para a penúltima. Os textos dos prompts ficam no código
(`prumo.prompts.catalog`), versionados com ele; o AppConfig guarda só QUAL versão está no ar,
o que dá deploy e rollback sem novo deploy de código.

Leitura pelo plano de dados (`appconfigdata`): a sessão devolve conteúdo só quando ele muda, então
o adaptador guarda o token e o último conteúdo, e respeita o intervalo mínimo de consulta.

Escrita pelo plano de controle (`appconfig`), com trava otimista. O estado de partida é lido no
próprio plano de controle (último deployment do ambiente → versão hospedada implantada), não no
cache do plano de dados, e a nova versão só é criada se nenhuma outra tiver sido criada depois da
implantada (`LatestVersionNumber`). Dois escritores concorrentes (o alarme e um clique manual,
ou o mesmo alarme entregue duas vezes) nunca partem do mesmo estado: um deles recebe ConflictError.
Efeito colateral consciente: uma versão criada e nunca implantada trava as escritas até alguém
implantá-la ou apagá-la; é a direção segura (recusar em vez de sobrescrever).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Self

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from prumo.adapters.aws._common import client_config, error_code, provider_error
from prumo.domain.errors import ConflictError, NotFoundError, ProviderError
from prumo.domain.models import PROMPT_VERSION_PATTERN, PromptVersion

if TYPE_CHECKING:
    from mypy_boto3_appconfig import AppConfigClient
    from mypy_boto3_appconfig.type_defs import DeploymentSummaryTypeDef
    from mypy_boto3_appconfigdata import AppConfigDataClient

    from prumo.config import Settings

logger = logging.getLogger(__name__)

DEFAULT_DEPLOYMENT_STRATEGY = "AppConfig.AllAtOnce"
"""Estratégia predefinida da AWS (id = nome), padrão de `Settings.appconfig_deployment_strategy`.
Ela tem 10 min de "bake": durante esse tempo um novo deploy no mesmo ambiente é recusado
(ConflictError). Para rollback imediato, use uma estratégia própria com bake 0 e passe o id dela
em PRUMO_APPCONFIG_DEPLOYMENT_STRATEGY."""

MIN_POLL_INTERVAL_SECONDS = 15
"""Menor intervalo aceito pelo AppConfig entre duas consultas da mesma sessão."""

WRITE_GRACE_SECONDS = 30
"""Depois de publicar, este processo usa o que escreveu por esse tempo (o deploy leva alguns
segundos para chegar ao plano de dados); depois abre sessão nova e lê a verdade do AppConfig,
inclusive se o deploy tiver falhado."""

MAX_HISTORY = 50
CONTENT_TYPE = "application/json"
SERVICE_DATA = "appconfigdata"
SERVICE_CONTROL = "appconfig"

IN_PROGRESS_STATES = frozenset({"DEPLOYING", "BAKING", "VALIDATING", "ROLLING_BACK"})
"""Um ambiente aceita um deployment por vez: escrever agora criaria uma versão que não sobe."""
REVERTED_STATES = frozenset({"ROLLED_BACK", "REVERTED"})
"""Deployments desfeitos: o que vale é o anterior a eles."""

VersionId = Annotated[str, Field(pattern=PROMPT_VERSION_PATTERN)]


@dataclass(frozen=True, slots=True)
class _ConflictRule:
    """Quais códigos de erro de uma chamada de escrita significam "conflito, tente de novo"."""

    codes: frozenset[str]
    message: str
    log_event: str


_DEPLOYMENT_IN_PROGRESS = _ConflictRule(
    codes=frozenset({"ConflictException"}),
    message="já há um deploy do AppConfig em andamento neste ambiente; tente de novo",
    log_event="appconfig_deployment_conflict",
)
_VERSION_LOCK = _ConflictRule(
    # A trava `LatestVersionNumber` volta como ConflictException ou BadRequestException.
    codes=frozenset({"ConflictException", "BadRequestException"}),
    message="a configuração do AppConfig mudou desde a leitura; tente de novo",
    log_event="appconfig_version_lock_conflict",
)


class PromptState(BaseModel):
    """O documento hospedado no AppConfig."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    active: VersionId
    history: list[VersionId] = Field(min_length=1, max_length=MAX_HISTORY)

    @model_validator(mode="after")
    def _history_ends_at_active(self) -> Self:
        if self.history[-1] != self.active:
            raise ValueError("history precisa terminar na versão ativa")
        return self


@dataclass(frozen=True, slots=True)
class AppConfigTarget:
    """Nomes (ou ids) da aplicação, do ambiente e do perfil de configuração."""

    application: str
    environment: str
    profile: str


@dataclass(frozen=True, slots=True)
class _ResolvedIds:
    application_id: str
    environment_id: str
    profile_id: str
    profile_name: str


@dataclass(frozen=True, slots=True)
class _DeployedState:
    """O estado implantado e o número da versão hospedada que o contém (a trava otimista)."""

    state: PromptState
    version_number: int


def parse_state(content: bytes) -> PromptState:
    try:
        return PromptState.model_validate_json(content)
    except ValidationError as exc:
        logger.error("appconfig_content_invalid", extra={"errors": exc.error_count()})
        raise ProviderError("conteúdo de prompt inválido no AppConfig") from exc


class AppConfigPromptStore:
    """Porta `PromptStore` sobre o AWS AppConfig."""

    def __init__(
        self,
        *,
        data_client: AppConfigDataClient,
        control_client: AppConfigClient,
        catalog: tuple[PromptVersion, ...],
        target: AppConfigTarget,
        deployment_strategy: str = DEFAULT_DEPLOYMENT_STRATEGY,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._data = data_client
        self._control = control_client
        self._catalog = {prompt.id: prompt for prompt in catalog}
        self._target = target
        self._strategy = deployment_strategy
        self._monotonic = monotonic
        self._token: str | None = None
        self._state: PromptState | None = None
        self._degraded = False
        self._next_poll_at = 0.0
        self._ids: _ResolvedIds | None = None
        # O token de sessão é de uso único: duas threads consultando juntas o invalidariam.
        self._lock = threading.Lock()

    @classmethod
    def from_settings(
        cls, settings: Settings, catalog: tuple[PromptVersion, ...]
    ) -> AppConfigPromptStore:
        config = client_config()
        return cls(
            data_client=boto3.client(
                "appconfigdata", region_name=settings.aws_region, config=config
            ),
            control_client=boto3.client(
                "appconfig", region_name=settings.aws_region, config=config
            ),
            catalog=catalog,
            target=AppConfigTarget(
                application=settings.appconfig_application,
                environment=settings.appconfig_environment,
                profile=settings.appconfig_profile,
            ),
            deployment_strategy=settings.appconfig_deployment_strategy,
        )

    def catalog(self) -> list[PromptVersion]:
        return list(self._catalog.values())

    def active(self) -> PromptVersion:
        with self._lock:
            return self._known(self._current_state().active)

    def previous(self) -> PromptVersion | None:
        with self._lock:
            history = self._current_state().history
        return self._known(history[-2]) if len(history) > 1 else None

    def deploy(self, version_id: str) -> PromptVersion:
        target = self._catalog.get(version_id)
        if target is None:
            raise NotFoundError("versão de prompt inexistente", details={"id": version_id})
        with self._lock:
            deployed = self._deployed_state()
            state = deployed.state
            if state.active == version_id:
                raise ConflictError("essa versão já está em produção", details={"id": version_id})
            history = [*state.history, version_id][-MAX_HISTORY:]
            self._publish(
                PromptState(active=version_id, history=history),
                f"deploy {version_id}",
                based_on=deployed.version_number,
            )
        return target

    def rollback(self, expected_active: str | None = None) -> PromptVersion:
        with self._lock:
            deployed = self._deployed_state()
            state = deployed.state
            if expected_active is not None and state.active != expected_active:
                details = {"expected": expected_active, "active": state.active}
                logger.warning("rollback_skipped_version_changed", extra=details)
                raise ConflictError(
                    "a versão no ar mudou desde a leitura; rollback cancelado", details=details
                )
            if len(state.history) < 2:
                raise ConflictError("não há versão anterior para voltar")
            history = state.history[:-1]
            restored = self._known(history[-1])
            self._publish(
                PromptState(active=restored.id, history=history),
                f"rollback {state.active} -> {restored.id}",
                based_on=deployed.version_number,
            )
        return restored

    # Leitura (plano de dados) ----------------------------------------------------------------

    def _current_state(self) -> PromptState:
        """Estado em cache; consulta o AppConfig só quando o intervalo de consulta venceu."""
        if self._state is not None and self._monotonic() < self._next_poll_at:
            return self._cached_state()
        try:
            content = self._fetch_latest()
            if content:
                self._state = parse_state(content)
                self._degraded = False
        except ProviderError:
            # Sessão nova na próxima consulta: com o token já avançado, um conteúdo inválido
            # nunca seria relido (a sessão só devolve conteúdo quando ele muda).
            self._token = None
            if self._state is None:
                raise
            # Degrada para o último estado válido em vez de parar a classificação de leads.
            self._degraded = True
            self._next_poll_at = self._monotonic() + MIN_POLL_INTERVAL_SECONDS
        return self._cached_state()

    def _cached_state(self) -> PromptState:
        if self._state is None:
            raise ProviderError("o AppConfig devolveu configuração de prompt vazia")
        if self._degraded:
            logger.warning("appconfig_using_stale_cache", extra={"active": self._state.active})
        return self._state

    def _fetch_latest(self) -> bytes:
        """Consulta o plano de dados. Devolve b"" quando nada mudou desde a última consulta."""
        if self._token is None:
            self._token = self._start_session()
        try:
            response = self._data.get_latest_configuration(ConfigurationToken=self._token)
            content = response["Configuration"].read()
        except (ClientError, BotoCoreError) as exc:
            # Token expirado ou inválido não se recupera: a próxima leitura abre outra sessão.
            self._token = None
            raise provider_error(
                exc, service=SERVICE_DATA, operation="GetLatestConfiguration"
            ) from exc
        self._token = response["NextPollConfigurationToken"]
        interval = response.get("NextPollIntervalInSeconds") or MIN_POLL_INTERVAL_SECONDS
        self._next_poll_at = self._monotonic() + interval
        return content

    def _start_session(self) -> str:
        try:
            response = self._data.start_configuration_session(
                ApplicationIdentifier=self._target.application,
                EnvironmentIdentifier=self._target.environment,
                ConfigurationProfileIdentifier=self._target.profile,
                RequiredMinimumPollIntervalInSeconds=MIN_POLL_INTERVAL_SECONDS,
            )
        except (ClientError, BotoCoreError) as exc:
            raise provider_error(
                exc, service=SERVICE_DATA, operation="StartConfigurationSession"
            ) from exc
        return response["InitialConfigurationToken"]

    def _known(self, version_id: str) -> PromptVersion:
        prompt = self._catalog.get(version_id)
        if prompt is None:
            logger.error("appconfig_version_not_in_catalog", extra={"version": version_id})
            raise ProviderError(
                "a versão ativa no AppConfig não existe no catálogo", details={"id": version_id}
            )
        return prompt

    # Estado implantado (plano de controle) -----------------------------------------------------

    def _deployed_state(self) -> _DeployedState:
        """O que está implantado agora, lido no plano de controle (a base de toda escrita)."""
        ids = self._resolve_ids()
        version = _version_number(self._latest_deployment(ids))
        content = self._control_call(
            "GetHostedConfigurationVersion",
            lambda: self._control.get_hosted_configuration_version(
                ApplicationId=ids.application_id,
                ConfigurationProfileId=ids.profile_id,
                VersionNumber=version,
            )["Content"].read(),
        )
        return _DeployedState(state=parse_state(content), version_number=version)

    def _latest_deployment(self, ids: _ResolvedIds) -> DeploymentSummaryTypeDef:
        """Deployment mais recente deste perfil que não foi desfeito.

        A AWS lista do mais novo para o mais antigo; o primeiro da lista diz se há deployment
        em andamento no ambiente (de qualquer perfil).
        """
        try:
            for position, item in enumerate(self._deployments(ids)):
                if position == 0:
                    _ensure_settled(item)
                if _is_effective(item, ids):
                    return item
        except (ClientError, BotoCoreError) as exc:
            raise provider_error(exc, service=SERVICE_CONTROL, operation="ListDeployments") from exc
        logger.error("appconfig_no_deployment", extra={"profile": ids.profile_id})
        raise ProviderError(
            "nenhuma configuração de prompt implantada neste ambiente",
            details={"operation": "ListDeployments"},
        )

    def _deployments(self, ids: _ResolvedIds) -> Iterator[DeploymentSummaryTypeDef]:
        paginator = self._control.get_paginator("list_deployments")
        for page in paginator.paginate(
            ApplicationId=ids.application_id, EnvironmentId=ids.environment_id
        ):
            yield from page.get("Items", [])

    # Escrita (plano de controle) ---------------------------------------------------------------

    def _publish(self, state: PromptState, description: str, *, based_on: int) -> None:
        ids = self._resolve_ids()
        created = self._control_call(
            "CreateHostedConfigurationVersion",
            lambda: self._control.create_hosted_configuration_version(
                ApplicationId=ids.application_id,
                ConfigurationProfileId=ids.profile_id,
                Content=state.model_dump_json().encode(),
                ContentType=CONTENT_TYPE,
                Description=description,
                LatestVersionNumber=based_on,
            ),
            conflict=_VERSION_LOCK,
        )
        deployment = self._control_call(
            "StartDeployment",
            lambda: self._control.start_deployment(
                ApplicationId=ids.application_id,
                EnvironmentId=ids.environment_id,
                DeploymentStrategyId=self._strategy,
                ConfigurationProfileId=ids.profile_id,
                ConfigurationVersion=str(created["VersionNumber"]),
                Description=description,
            ),
            conflict=_DEPLOYMENT_IN_PROGRESS,
        )
        self._state = state
        self._degraded = False
        self._token = None
        self._next_poll_at = self._monotonic() + WRITE_GRACE_SECONDS
        logger.info(
            "appconfig_deployment_started",
            extra={
                "active": state.active,
                "based_on_version": based_on,
                "configuration_version": created["VersionNumber"],
                "deployment_number": deployment["DeploymentNumber"],
                "strategy": self._strategy,
            },
        )

    def _control_call[T](
        self, operation: str, call: Callable[[], T], *, conflict: _ConflictRule | None = None
    ) -> T:
        try:
            return call()
        except ClientError as exc:
            code = error_code(exc)
            if conflict is not None and code in conflict.codes:
                details = {"operation": operation, "error_code": code}
                logger.warning(conflict.log_event, extra=details)
                raise ConflictError(conflict.message, details=details) from exc
            raise provider_error(exc, service=SERVICE_CONTROL, operation=operation) from exc
        except BotoCoreError as exc:
            raise provider_error(exc, service=SERVICE_CONTROL, operation=operation) from exc

    def _resolve_ids(self) -> _ResolvedIds:
        """O plano de controle exige ids; as configurações aceitam nomes. Resolve uma vez."""
        if self._ids is None:
            target = self._target
            app_id, _ = self._find("ListApplications", self._applications(), target.application)
            env_id, _ = self._find(
                "ListEnvironments", self._environments(app_id), target.environment
            )
            profile_id, profile_name = self._find(
                "ListConfigurationProfiles", self._profiles(app_id), target.profile
            )
            self._ids = _ResolvedIds(app_id, env_id, profile_id, profile_name)
        return self._ids

    def _applications(self) -> Iterator[tuple[str, str]]:
        for page in self._control.get_paginator("list_applications").paginate():
            for item in page.get("Items", []):
                yield item.get("Id", ""), item.get("Name", "")

    def _environments(self, application_id: str) -> Iterator[tuple[str, str]]:
        paginator = self._control.get_paginator("list_environments")
        for page in paginator.paginate(ApplicationId=application_id):
            for item in page.get("Items", []):
                yield item.get("Id", ""), item.get("Name", "")

    def _profiles(self, application_id: str) -> Iterator[tuple[str, str]]:
        paginator = self._control.get_paginator("list_configuration_profiles")
        for page in paginator.paginate(ApplicationId=application_id):
            for item in page.get("Items", []):
                yield item.get("Id", ""), item.get("Name", "")

    def _find(
        self, operation: str, candidates: Iterable[tuple[str, str]], wanted: str
    ) -> tuple[str, str]:
        """Acha (id, nome) pelo nome ou pelo próprio id.

        `candidates` é um gerador: as chamadas à AWS acontecem durante a iteração, aqui dentro,
        e por isso ficam cobertas pelo tratamento de erro.
        """
        try:
            for item_id, name in candidates:
                if wanted in (item_id, name):
                    return item_id, name
        except (ClientError, BotoCoreError) as exc:
            raise provider_error(exc, service=SERVICE_CONTROL, operation=operation) from exc
        logger.error(
            "appconfig_resource_not_found", extra={"operation": operation, "wanted": wanted}
        )
        raise ProviderError(
            "recurso do AppConfig não encontrado",
            details={"operation": operation, "wanted": wanted},
        )


def _ensure_settled(newest: DeploymentSummaryTypeDef) -> None:
    state = newest.get("State", "")
    if state in IN_PROGRESS_STATES:
        details = {"deployment_number": newest.get("DeploymentNumber"), "state": state}
        logger.warning("appconfig_deployment_in_progress", extra=details)
        raise ConflictError(_DEPLOYMENT_IN_PROGRESS.message, details=details)


def _is_effective(item: DeploymentSummaryTypeDef, ids: _ResolvedIds) -> bool:
    ours = item.get("ConfigurationProfileId") == ids.profile_id or (
        item.get("ConfigurationName") == ids.profile_name
    )
    return ours and item.get("State", "") not in REVERTED_STATES


def _version_number(deployment: DeploymentSummaryTypeDef) -> int:
    raw = deployment.get("ConfigurationVersion", "")
    if raw.isdigit():
        return int(raw)
    details = {"deployment_number": deployment.get("DeploymentNumber")}
    logger.error("appconfig_deployment_version_invalid", extra=details)
    raise ProviderError("versão implantada ilegível no AppConfig", details=details)

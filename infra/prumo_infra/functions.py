"""As quatro Lambdas do Prumo: mesmo pacote (build/lambda), handlers e limites diferentes."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from aws_cdk import Duration, RemovalPolicy
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from constructs import Construct

from prumo_infra.config import LOG_RETENTION, ConfigError


@dataclass(frozen=True, slots=True)
class FunctionSpec:
    """O que diferencia cada Lambda. O resto (runtime, arquitetura, tracing) é comum."""

    construct_id: str
    handler: str
    description: str
    memory_mb: int
    timeout: Duration
    async_retries: int | None = None
    """Só para invocação assíncrona (agendador/EventBridge); None mantém o padrão da Lambda."""


# A API fica abaixo dos 30 s de integração do API Gateway para responder antes dele cortar.
API = FunctionSpec(
    "ApiFunction", "prumo.lambdas.api.handler", "API HTTP do Prumo", 512, Duration.seconds(29)
)
ESTIMATOR = FunctionSpec(
    "EstimatorFunction",
    "prumo.lambdas.estimator.handler",
    "Leitura horária da qualidade (publica a métrica SloBreached)",
    512,
    Duration.minutes(2),
    # Uma tentativa extra cabe na mesma hora; leituras duplicadas não mudam o Maximum do alarme.
    async_retries=1,
)
ROLLBACK = FunctionSpec(
    "RollbackFunction",
    "prumo.lambdas.rollback.handler",
    "Volta o prompt para a versão anterior quando o alarme de SLO dispara",
    256,
    Duration.minutes(1),
    # O alarme fica em ALARM e não gera outro evento: sem retry, uma falha transitória deixaria
    # o prompt ruim no ar. Repetir é seguro porque o handler confere que a versão ativa ainda
    # está abaixo da meta antes de reverter (uma repetição pós-rollback encontra a restaurada).
    async_retries=2,
)
REVIEW_TASK = FunctionSpec(
    "ReviewTaskFunction",
    "prumo.lambdas.review_task.handler",
    "Guarda o task token da revisão humana (Step Functions waitForTaskToken)",
    256,
    Duration.seconds(30),
)
ALL_SPECS = (API, ESTIMATOR, ROLLBACK, REVIEW_TASK)


def lambda_code(asset_path: Path) -> lambda_.Code:
    """Valida o bundle antes de empacotar: um handler ausente só apareceria em produção."""
    package = asset_path / "prumo"
    if not (package / "__init__.py").is_file():
        raise ConfigError(
            f"bundle das Lambdas ausente em {asset_path}: rode `bash scripts/build_lambda.sh`"
        )
    missing = [s.handler for s in ALL_SPECS if not _handler_module(asset_path, s.handler).is_file()]
    if missing:
        raise ConfigError(f"handlers ausentes no bundle {asset_path}: {', '.join(missing)}")
    return lambda_.Code.from_asset(str(asset_path))


def _handler_module(asset_path: Path, handler: str) -> Path:
    module = handler.rsplit(".", 1)[0]
    return asset_path.joinpath(*module.split(".")).with_suffix(".py")


def build_function(
    scope: Construct,
    spec: FunctionSpec,
    *,
    code: lambda_.Code,
    environment: Mapping[str, str],
    removal_policy: RemovalPolicy,
    on_failure: lambda_.IDestination | None = None,
) -> lambda_.Function:
    """Lambda Python 3.12 arm64 com X-Ray ativo e log group com retenção definida.

    Sem reserved concurrency de propósito: conta nova tem cota de 10 execuções simultâneas e a
    AWS recusa reservar abaixo do mínimo livre. O rollback não precisa dela para não correr em
    paralelo: o compare-and-swap do PromptStore já serializa.
    """
    log_group = logs.LogGroup(
        scope,
        f"{spec.construct_id}Logs",
        retention=LOG_RETENTION,
        removal_policy=removal_policy,
    )
    return lambda_.Function(
        scope,
        spec.construct_id,
        runtime=lambda_.Runtime.PYTHON_3_12,
        architecture=lambda_.Architecture.ARM_64,
        handler=spec.handler,
        code=code,
        description=spec.description,
        memory_size=spec.memory_mb,
        timeout=spec.timeout,
        environment=dict(environment),
        tracing=lambda_.Tracing.ACTIVE,
        log_group=log_group,
        retry_attempts=spec.async_retries,
        on_failure=on_failure,
        recursive_loop=lambda_.RecursiveLoop.TERMINATE,
    )

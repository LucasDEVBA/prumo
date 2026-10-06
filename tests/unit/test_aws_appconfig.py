import io
import json
import logging

import boto3
import pytest
from botocore.response import StreamingBody
from botocore.stub import Stubber

from prumo.adapters.aws.appconfig import (
    DEFAULT_DEPLOYMENT_STRATEGY,
    MIN_POLL_INTERVAL_SECONDS,
    WRITE_GRACE_SECONDS,
    AppConfigPromptStore,
    AppConfigTarget,
)
from prumo.config import PromptBackend, Settings
from prumo.domain.errors import ConflictError, NotFoundError, ProviderError
from prumo.prompts.catalog import PROMPTS

REGION = "us-east-1"
TARGET = AppConfigTarget(application="prumo", environment="prod", profile="prompt-version")
APP_ID, ENV_ID, PROFILE_ID = "abc1234", "env5678", "prf9012"
DEPLOYED_VERSION = 7
CREATED_VERSION = 8


@pytest.fixture(autouse=True)
def fake_aws_credentials(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.delenv("AWS_PROFILE", raising=False)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def document(active: str, history: list[str]) -> bytes:
    return json.dumps({"active": active, "history": history}).encode()


def compact(active: str, history: list[str]) -> bytes:
    return json.dumps({"active": active, "history": history}, separators=(",", ":")).encode()


def stream(content: bytes) -> StreamingBody:
    return StreamingBody(io.BytesIO(content), len(content))


def deployment(
    number: int,
    version: int,
    *,
    state: str = "COMPLETE",
    profile_id: str = PROFILE_ID,
    name: str = "prompt-version",
) -> dict:
    return {
        "DeploymentNumber": number,
        "ConfigurationProfileId": profile_id,
        "ConfigurationName": name,
        "ConfigurationVersion": str(version),
        "State": state,
    }


class Harness:
    def __init__(self) -> None:
        self.clock = FakeClock()
        self.data_client = boto3.client("appconfigdata", region_name=REGION)
        self.control_client = boto3.client("appconfig", region_name=REGION)
        self.data = Stubber(self.data_client)
        self.control = Stubber(self.control_client)
        self.store = AppConfigPromptStore(
            data_client=self.data_client,
            control_client=self.control_client,
            catalog=PROMPTS,
            target=TARGET,
            monotonic=self.clock,
        )
        self._sessions = 0

    # Plano de dados ----------------------------------------------------------------------

    def expect_session(self) -> str:
        self._sessions += 1
        token = f"initial-{self._sessions}"
        self.data.add_response(
            "start_configuration_session",
            {"InitialConfigurationToken": token},
            {
                "ApplicationIdentifier": "prumo",
                "EnvironmentIdentifier": "prod",
                "ConfigurationProfileIdentifier": "prompt-version",
                "RequiredMinimumPollIntervalInSeconds": MIN_POLL_INTERVAL_SECONDS,
            },
        )
        return token

    def expect_poll(self, token: str, content: bytes, next_token: str, interval: int = 30) -> None:
        self.data.add_response(
            "get_latest_configuration",
            {
                "NextPollConfigurationToken": next_token,
                "NextPollIntervalInSeconds": interval,
                "ContentType": "application/json",
                "Configuration": stream(content),
            },
            {"ConfigurationToken": token},
        )

    # Plano de controle -------------------------------------------------------------------

    def expect_id_lookup(self) -> None:
        self.control.add_response(
            "list_applications",
            {"Items": [{"Id": "zzz0000", "Name": "outra"}, {"Id": APP_ID, "Name": "prumo"}]},
        )
        self.control.add_response(
            "list_environments",
            {"Items": [{"Id": ENV_ID, "Name": "prod", "ApplicationId": APP_ID}]},
            {"ApplicationId": APP_ID},
        )
        self.control.add_response(
            "list_configuration_profiles",
            {"Items": [{"Id": PROFILE_ID, "Name": "prompt-version", "ApplicationId": APP_ID}]},
            {"ApplicationId": APP_ID},
        )

    def expect_deployments(self, items: list[dict], next_page: str | None = None) -> None:
        response: dict = {"Items": items}
        params: dict = {"ApplicationId": APP_ID, "EnvironmentId": ENV_ID}
        if next_page:
            response["NextToken"] = next_page
        self.control.add_response("list_deployments", response, params)

    def expect_hosted(self, version: int, content: bytes) -> None:
        self.control.add_response(
            "get_hosted_configuration_version",
            {
                "ApplicationId": APP_ID,
                "ConfigurationProfileId": PROFILE_ID,
                "VersionNumber": version,
                "ContentType": "application/json",
                "Content": stream(content),
            },
            {
                "ApplicationId": APP_ID,
                "ConfigurationProfileId": PROFILE_ID,
                "VersionNumber": version,
            },
        )

    def expect_deployed(
        self, active: str, history: list[str], version: int = DEPLOYED_VERSION
    ) -> None:
        """O estado implantado, lido no plano de controle (base de toda escrita)."""
        self.expect_deployments([deployment(3, version)])
        self.expect_hosted(version, document(active, history))

    def expect_create(self, active: str, history: list[str], description: str, latest: int):
        self.control.add_response(
            "create_hosted_configuration_version",
            {
                "ApplicationId": APP_ID,
                "ConfigurationProfileId": PROFILE_ID,
                "VersionNumber": CREATED_VERSION,
            },
            {
                "ApplicationId": APP_ID,
                "ConfigurationProfileId": PROFILE_ID,
                "Content": compact(active, history),
                "ContentType": "application/json",
                "Description": description,
                "LatestVersionNumber": latest,
            },
        )

    def expect_publish(
        self,
        active: str,
        history: list[str],
        description: str,
        latest: int = DEPLOYED_VERSION,
    ) -> None:
        self.expect_create(active, history, description, latest)
        self.control.add_response(
            "start_deployment",
            {"DeploymentNumber": 4, "State": "DEPLOYING"},
            {
                "ApplicationId": APP_ID,
                "EnvironmentId": ENV_ID,
                "DeploymentStrategyId": DEFAULT_DEPLOYMENT_STRATEGY,
                "ConfigurationProfileId": PROFILE_ID,
                "ConfigurationVersion": str(CREATED_VERSION),
                "Description": description,
            },
        )


@pytest.fixture
def aws():
    harness = Harness()
    with harness.data, harness.control:
        yield harness
        harness.data.assert_no_pending_responses()
        harness.control.assert_no_pending_responses()


# Leitura ---------------------------------------------------------------------------------


def test_should_read_active_version_once_and_serve_it_from_cache(aws):
    aws.expect_poll(aws.expect_session(), document("v2", ["v1", "v2"]), "next-1")

    assert aws.store.active().id == "v2"
    assert aws.store.previous().id == "v1"
    aws.clock.advance(29)
    assert aws.store.active().id == "v2"  # dentro do intervalo: nenhuma chamada nova


def test_should_keep_last_content_when_poll_says_nothing_changed(aws):
    aws.expect_poll(aws.expect_session(), document("v2", ["v1", "v2"]), "next-1")
    aws.expect_poll("next-1", b"", "next-2")
    aws.expect_poll("next-2", document("v1", ["v1"]), "next-3")

    assert aws.store.active().id == "v2"
    aws.clock.advance(30)
    assert aws.store.active().id == "v2"
    aws.clock.advance(30)
    assert aws.store.active().id == "v1"
    assert aws.store.previous() is None


def test_should_serve_last_known_version_when_poll_fails_and_reopen_session_later(aws):
    aws.expect_poll(aws.expect_session(), document("v2", ["v1", "v2"]), "next-1")
    aws.data.add_client_error("get_latest_configuration", service_error_code="BadRequestException")
    aws.expect_poll(aws.expect_session(), document("v3", ["v2", "v3"]), "next-2")

    assert aws.store.active().id == "v2"
    aws.clock.advance(30)
    assert aws.store.active().id == "v2"
    aws.clock.advance(MIN_POLL_INTERVAL_SECONDS)
    assert aws.store.active().id == "v3"


def test_should_keep_last_valid_version_when_new_content_is_invalid(aws):
    aws.expect_poll(aws.expect_session(), document("v2", ["v1", "v2"]), "next-1")
    aws.expect_poll("next-1", b'{"active": "v3"}', "next-2")

    assert aws.store.active().id == "v2"
    aws.clock.advance(30)
    assert aws.store.active().id == "v2"


def test_should_reread_after_invalid_content(aws):
    """Conteúdo inválido abre sessão nova: a sessão antiga nunca mais devolveria o conteúdo."""
    aws.expect_poll(aws.expect_session(), document("v2", ["v1", "v2"]), "next-1")
    aws.expect_poll("next-1", b'{"active": "v3"}', "next-2")
    aws.expect_poll(aws.expect_session(), document("v3", ["v2", "v3"]), "next-3")

    assert aws.store.active().id == "v2"
    aws.clock.advance(30)
    assert aws.store.active().id == "v2"  # degradado: último estado válido
    aws.clock.advance(MIN_POLL_INTERVAL_SECONDS)
    assert aws.store.active().id == "v3"  # releu do zero e saiu do modo degradado


def test_should_warn_on_every_use_of_the_degraded_cache(aws, caplog):
    aws.expect_poll(aws.expect_session(), document("v2", ["v1", "v2"]), "next-1")
    aws.expect_poll("next-1", b"nao-e-json", "next-2")
    aws.expect_poll(aws.expect_session(), document("v2", ["v1", "v2"]), "next-3")

    assert aws.store.active().id == "v2"
    aws.clock.advance(30)
    with caplog.at_level(logging.WARNING):
        aws.store.active()
        aws.store.active()
        aws.store.previous()
    assert caplog.messages.count("appconfig_using_stale_cache") == 3

    caplog.clear()
    aws.clock.advance(MIN_POLL_INTERVAL_SECONDS)
    with caplog.at_level(logging.WARNING):
        aws.store.active()
    assert "appconfig_using_stale_cache" not in caplog.messages


def test_should_raise_provider_error_when_nothing_was_ever_read(aws):
    aws.data.add_client_error(
        "start_configuration_session", service_error_code="ResourceNotFoundException"
    )
    with pytest.raises(ProviderError) as caught:
        aws.store.active()
    assert caught.value.details["operation"] == "StartConfigurationSession"


@pytest.mark.parametrize(
    "content",
    [
        document("v2", ["v2", "v1"]),
        document("v2", []),
        b'{"active": "v2"}',
        b"nao-e-json",
        document("../v1", ["../v1"]),
    ],
)
def test_should_reject_invalid_hosted_content(aws, content):
    aws.expect_poll(aws.expect_session(), content, "next-1")
    with pytest.raises(ProviderError, match="inválido"):
        aws.store.active()


def test_should_fail_when_active_version_is_not_in_catalog(aws):
    aws.expect_poll(aws.expect_session(), document("v9", ["v9"]), "next-1")
    with pytest.raises(ProviderError, match="catálogo"):
        aws.store.active()


# Escrita ---------------------------------------------------------------------------------


def test_should_publish_new_version_and_deploy_it(aws):
    aws.expect_id_lookup()
    aws.expect_deployed("v1", ["v1"])
    aws.expect_publish("v2", ["v1", "v2"], "deploy v2")

    deployed = aws.store.deploy("v2")

    assert deployed.id == "v2"
    assert aws.store.active().id == "v2"  # lê o que escreveu, sem esperar a propagação
    assert aws.store.previous().id == "v1"


def test_should_base_writes_on_the_control_plane_not_on_the_cached_read(aws):
    aws.expect_poll(aws.expect_session(), document("v1", ["v1"]), "next-1")
    aws.expect_id_lookup()
    aws.expect_deployed("v2", ["v1", "v2"])  # outro processo já publicou v2
    aws.expect_publish("v3", ["v1", "v2", "v3"], "deploy v3")

    assert aws.store.active().id == "v1"  # cache do plano de dados ainda atrasado
    aws.store.deploy("v3")


def test_should_reread_from_appconfig_after_the_write_grace_period(aws):
    aws.expect_id_lookup()
    aws.expect_deployed("v1", ["v1"])
    aws.expect_publish("v2", ["v1", "v2"], "deploy v2")
    aws.expect_poll(aws.expect_session(), document("v1", ["v1"]), "next-9")

    aws.store.deploy("v2")
    aws.clock.advance(WRITE_GRACE_SECONDS)
    assert aws.store.active().id == "v1"  # o deploy não vingou: vale o que está no AppConfig


def test_should_roll_back_to_previous_version(aws):
    aws.expect_id_lookup()
    aws.expect_deployed("v3", ["v1", "v2", "v3"])
    aws.expect_publish("v2", ["v1", "v2"], "rollback v3 -> v2")

    assert aws.store.rollback().id == "v2"
    assert aws.store.active().id == "v2"


def test_should_roll_back_when_expected_version_is_still_active(aws):
    aws.expect_id_lookup()
    aws.expect_deployed("v3", ["v1", "v2", "v3"])
    aws.expect_publish("v2", ["v1", "v2"], "rollback v3 -> v2")

    assert aws.store.rollback(expected_active="v3").id == "v2"


def test_should_not_roll_back_when_active_version_changed_since_the_check(aws, caplog):
    aws.expect_id_lookup()
    aws.expect_deployed("v2", ["v1", "v2"])  # alguém já reverteu v3 -> v2

    with caplog.at_level(logging.WARNING), pytest.raises(ConflictError) as caught:
        aws.store.rollback(expected_active="v3")

    assert caught.value.details == {"expected": "v3", "active": "v2"}
    assert "rollback_skipped_version_changed" in caplog.messages


def test_should_resolve_ids_only_once(aws):
    aws.expect_id_lookup()
    aws.expect_deployed("v1", ["v1"])
    aws.expect_publish("v2", ["v1", "v2"], "deploy v2")
    aws.expect_deployed("v2", ["v1", "v2"], version=CREATED_VERSION)
    aws.expect_publish("v1", ["v1"], "rollback v2 -> v1", latest=CREATED_VERSION)

    aws.store.deploy("v2")
    aws.store.rollback()


def test_should_refuse_rollback_without_previous_version(aws):
    aws.expect_id_lookup()
    aws.expect_deployed("v1", ["v1"])
    with pytest.raises(ConflictError):
        aws.store.rollback()


def test_should_refuse_deploying_the_version_already_active(aws):
    aws.expect_id_lookup()
    aws.expect_deployed("v2", ["v1", "v2"])
    with pytest.raises(ConflictError):
        aws.store.deploy("v2")


def test_should_refuse_unknown_version_without_calling_aws(aws):
    with pytest.raises(NotFoundError):
        aws.store.deploy("v99")


@pytest.mark.parametrize("code", ["ConflictException", "BadRequestException"])
def test_should_translate_version_lock_failure_into_conflict(aws, code, caplog):
    aws.expect_id_lookup()
    aws.expect_deployed("v1", ["v1"])
    aws.control.add_client_error("create_hosted_configuration_version", service_error_code=code)

    with caplog.at_level(logging.WARNING), pytest.raises(ConflictError, match="mudou"):
        aws.store.deploy("v2")

    assert "appconfig_version_lock_conflict" in caplog.messages


def test_should_translate_deployment_in_progress_into_conflict(aws):
    aws.expect_id_lookup()
    aws.expect_deployed("v1", ["v1"])
    aws.expect_create("v2", ["v1", "v2"], "deploy v2", DEPLOYED_VERSION)
    aws.control.add_client_error("start_deployment", service_error_code="ConflictException")

    with pytest.raises(ConflictError, match="em andamento"):
        aws.store.deploy("v2")


@pytest.mark.parametrize("state", ["DEPLOYING", "BAKING", "VALIDATING", "ROLLING_BACK"])
def test_should_refuse_writing_while_a_deployment_is_in_progress(aws, state):
    """Sem isso a versão seria criada e o StartDeployment recusado: versão órfã travando tudo."""
    aws.expect_id_lookup()
    aws.expect_deployments([deployment(5, 9, state=state), deployment(4, DEPLOYED_VERSION)])

    with pytest.raises(ConflictError, match="em andamento") as caught:
        aws.store.rollback(expected_active="v2")
    assert caught.value.details == {"deployment_number": 5, "state": state}


def test_should_skip_reverted_and_foreign_deployments_across_pages(aws):
    aws.expect_id_lookup()
    aws.expect_deployments(
        [
            deployment(9, 12, state="ROLLED_BACK"),
            deployment(8, 3, profile_id="flags01", name="feature-flags"),
        ],
        next_page="page-2",
    )
    aws.control.add_response(
        "list_deployments",
        {"Items": [deployment(7, 11, state="REVERTED"), deployment(6, 10)]},
        {"ApplicationId": APP_ID, "EnvironmentId": ENV_ID, "NextToken": "page-2"},
    )
    aws.expect_hosted(10, document("v2", ["v1", "v2"]))
    aws.expect_publish("v1", ["v1"], "rollback v2 -> v1", latest=10)

    assert aws.store.rollback(expected_active="v2").id == "v1"


def test_should_match_deployment_by_profile_name_when_id_is_missing(aws):
    aws.expect_id_lookup()
    legacy = deployment(3, DEPLOYED_VERSION)
    del legacy["ConfigurationProfileId"]
    aws.expect_deployments([legacy])
    aws.expect_hosted(DEPLOYED_VERSION, document("v1", ["v1"]))
    aws.expect_publish("v2", ["v1", "v2"], "deploy v2")

    aws.store.deploy("v2")


def test_should_raise_provider_error_when_nothing_was_ever_deployed(aws):
    aws.expect_id_lookup()
    aws.expect_deployments([deployment(1, 3, state="ROLLED_BACK")])

    with pytest.raises(ProviderError, match="implantada"):
        aws.store.deploy("v2")


def test_should_raise_provider_error_when_deployed_version_is_unreadable(aws):
    aws.expect_id_lookup()
    aws.expect_deployments([{**deployment(3, 1), "ConfigurationVersion": "abc"}])

    with pytest.raises(ProviderError, match="ilegível"):
        aws.store.deploy("v2")


def test_should_raise_provider_error_when_listing_deployments_fails(aws):
    aws.expect_id_lookup()
    aws.control.add_client_error("list_deployments", service_error_code="AccessDeniedException")

    with pytest.raises(ProviderError) as caught:
        aws.store.deploy("v2")
    assert caught.value.details["operation"] == "ListDeployments"


def test_should_raise_provider_error_when_deployed_content_is_invalid(aws):
    aws.expect_id_lookup()
    aws.expect_deployments([deployment(3, DEPLOYED_VERSION)])
    aws.expect_hosted(DEPLOYED_VERSION, b'{"active": "v1"}')

    with pytest.raises(ProviderError, match="inválido"):
        aws.store.deploy("v2")


def test_should_raise_provider_error_when_environment_does_not_exist(aws):
    aws.control.add_response("list_applications", {"Items": [{"Id": APP_ID, "Name": "prumo"}]})
    aws.control.add_response("list_environments", {"Items": []}, {"ApplicationId": APP_ID})

    with pytest.raises(ProviderError) as caught:
        aws.store.deploy("v2")
    assert caught.value.details == {"operation": "ListEnvironments", "wanted": "prod"}


def test_should_raise_provider_error_when_listing_fails(aws):
    aws.control.add_client_error("list_applications", service_error_code="AccessDeniedException")

    with pytest.raises(ProviderError) as caught:
        aws.store.deploy("v2")
    assert caught.value.details["error_code"] == "AccessDeniedException"


def test_should_list_the_whole_catalog(aws):
    assert [p.id for p in aws.store.catalog()] == [p.id for p in PROMPTS]


def test_should_read_deployment_strategy_from_settings(monkeypatch):
    settings = Settings(prompt_backend=PromptBackend.APPCONFIG, aws_region="sa-east-1")
    assert AppConfigPromptStore.from_settings(settings, PROMPTS)._strategy == (
        DEFAULT_DEPLOYMENT_STRATEGY
    )

    monkeypatch.setenv("PRUMO_APPCONFIG_DEPLOYMENT_STRATEGY", "q1w2e3r")
    store = AppConfigPromptStore.from_settings(
        Settings(prompt_backend=PromptBackend.APPCONFIG, aws_region="sa-east-1"), PROMPTS
    )

    assert store._strategy == "q1w2e3r"
    assert store._target == TARGET
    assert store._data.meta.region_name == "sa-east-1"
    assert store._control.meta.config.retries["mode"] == "adaptive"

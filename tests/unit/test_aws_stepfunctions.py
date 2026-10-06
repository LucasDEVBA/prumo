import json
import logging
from datetime import UTC, datetime

import boto3
import pytest
from botocore.exceptions import ConnectTimeoutError
from botocore.stub import Stubber

from prumo.adapters.aws.stepfunctions import StepFunctionsReviewWorkflow, send_review_result
from prumo.config import Settings, Storage
from prumo.domain.errors import ProviderError
from prumo.domain.models import Decision, LeadClass

REGION = "us-east-1"
ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:prumo-review"
DECISION = Decision(
    id="0199a000-0000-7000-8000-000000000001",
    lead_text="Sou professora.",
    prompt_version="v1",
    predicted=LeadClass.MORNO,
    judge_approved=True,
    sampled_for_review=True,
    created_at=datetime(2026, 10, 1, tzinfo=UTC),
)
LABELED = DECISION.model_copy(update={"human_correct": False, "reviewer": "ana"})


@pytest.fixture(autouse=True)
def fake_aws_credentials(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.delenv("AWS_PROFILE", raising=False)


TASK_TOKEN = "task-token-1"  # noqa: S105 (valor falso de teste)


class FakeTokens:
    def __init__(self, token: str | None = TASK_TOKEN) -> None:
        self.token = token

    def save_task_token(self, decision_id: str, task_token: str) -> None:
        self.token = task_token

    def task_token_for(self, decision_id: str) -> str | None:
        return self.token


@pytest.fixture
def sfn():
    client = boto3.client("stepfunctions", region_name=REGION)
    stubber = Stubber(client)
    with stubber:
        yield client, stubber
        stubber.assert_no_pending_responses()


def workflow(client, tokens: FakeTokens | None = None) -> StepFunctionsReviewWorkflow:
    return StepFunctionsReviewWorkflow(
        client=client, state_machine_arn=ARN, token_store=tokens or FakeTokens()
    )


def test_should_start_one_execution_named_after_the_decision(sfn):
    client, stubber = sfn
    stubber.add_response(
        "start_execution",
        {"executionArn": f"{ARN}:{DECISION.id}", "startDate": datetime(2026, 10, 1, tzinfo=UTC)},
        {
            "stateMachineArn": ARN,
            "name": DECISION.id,
            "input": json.dumps({"decision_id": DECISION.id}),
        },
    )
    workflow(client).request_review(DECISION)


def test_should_treat_existing_execution_as_success(sfn):
    client, stubber = sfn
    stubber.add_client_error("start_execution", service_error_code="ExecutionAlreadyExists")
    workflow(client).request_review(DECISION)


def test_should_convert_other_start_errors_into_provider_error(sfn):
    client, stubber = sfn
    stubber.add_client_error("start_execution", service_error_code="StateMachineDoesNotExist")
    with pytest.raises(ProviderError) as caught:
        workflow(client).request_review(DECISION)
    assert caught.value.details["error_code"] == "StateMachineDoesNotExist"


def test_should_send_task_success_with_the_label(sfn):
    client, stubber = sfn
    stubber.add_response(
        "send_task_success",
        {},
        {
            "taskToken": "task-token-1",
            "output": json.dumps(
                {"decision_id": DECISION.id, "human_correct": False, "reviewer": "ana"}
            ),
        },
    )
    workflow(client).complete_review(LABELED)


def test_should_only_warn_when_token_was_not_saved_yet(sfn, caplog):
    client, _stubber = sfn
    with caplog.at_level(logging.WARNING):
        workflow(client, FakeTokens(token=None)).complete_review(LABELED)
    assert "review_task_token_missing" in caplog.messages


@pytest.mark.parametrize("code", ["TaskTimedOut", "InvalidToken", "TaskDoesNotExist"])
def test_should_only_warn_when_task_token_is_stale(sfn, caplog, code):
    client, stubber = sfn
    stubber.add_client_error("send_task_success", service_error_code=code)
    with caplog.at_level(logging.WARNING):
        workflow(client).complete_review(LABELED)
    assert "review_task_token_stale" in caplog.messages
    assert all("task-token-1" not in record.getMessage() for record in caplog.records)


def test_should_raise_provider_error_on_unexpected_send_failure(sfn):
    client, stubber = sfn
    stubber.add_client_error("send_task_success", service_error_code="ThrottlingException")
    with pytest.raises(ProviderError):
        workflow(client).complete_review(LABELED)


def test_should_raise_provider_error_on_network_failure():
    class Unreachable:
        def send_task_success(self, **_kwargs):
            raise ConnectTimeoutError(endpoint_url="https://states")

    with pytest.raises(ProviderError) as caught:
        send_review_result(Unreachable(), "task-token-1", LABELED)
    assert caught.value.details["error_code"] == "ConnectTimeoutError"


def test_should_require_state_machine_arn_in_settings():
    with pytest.raises(ValueError, match="PRUMO_REVIEW_STATE_MACHINE_ARN"):
        StepFunctionsReviewWorkflow.from_settings(Settings(review_state_machine_arn=None))


def test_should_require_dynamodb_storage_to_keep_task_tokens():
    with pytest.raises(ValueError, match="PRUMO_STORAGE=dynamodb"):
        StepFunctionsReviewWorkflow.from_settings(
            Settings(review_state_machine_arn=ARN, storage=Storage.MEMORY)
        )


def test_should_build_from_settings_with_dynamo_token_store():
    settings = Settings(
        review_state_machine_arn=ARN, aws_region="sa-east-1", storage=Storage.DYNAMODB
    )
    built = StepFunctionsReviewWorkflow.from_settings(settings)
    assert built._state_machine_arn == ARN
    assert built._client.meta.region_name == "sa-east-1"
    assert type(built._tokens).__name__ == "DynamoDecisionRepository"

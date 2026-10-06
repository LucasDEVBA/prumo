import asyncio
import importlib
import json
import logging
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from prumo.config import Settings
from prumo.container import Container, build_container
from prumo.domain.errors import InvalidInputError, NotFoundError
from prumo.domain.models import Decision, EventKind, LeadClass
from prumo.lambdas import estimator, review_task, rollback
from prumo.lambdas._runtime import bind_invocation
from prumo.observability import correlation_id

START = datetime(2026, 10, 1, tzinfo=UTC)
ALARM_EVENT = {
    "id": "evt-1",
    "detail-type": "CloudWatch Alarm State Change",
    "source": "aws.cloudwatch",
    "detail": {
        "alarmName": "prumo-slo-breached",
        "state": {"value": "ALARM", "reason": "Threshold Crossed"},
        "previousState": {"value": "OK"},
    },
}


@pytest.fixture(autouse=True)
def restore_logging():
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


@pytest.fixture
def container() -> Container:
    """Como na Lambda: o serviço não reverte sozinho; quem decide é o RollbackGuard."""
    built = build_container(Settings(simulation_warmup_hours=0))
    built.quality.policy = replace(built.quality.policy, auto_rollback=False)
    return built


def breach(container: Container, version: str, labels: int = 40) -> None:
    """Grava decisões erradas (pelo juiz e pelo humano) até a faixa ficar toda abaixo da meta."""
    for index in range(labels):
        decision = Decision(
            id=f"{version}-{index}",
            lead_text="lead",
            prompt_version=version,
            predicted=LeadClass.QUENTE,
            judge_approved=False,
            sampled_for_review=True,
            created_at=START + timedelta(minutes=index),
        )
        container.repository.add(decision)
        container.repository.save_label(
            decision.model_copy(update={"human_correct": False, "reviewer": "ana"})
        )


def alarm(state: str = "ALARM") -> dict:
    return {**ALARM_EVENT, "detail": {**ALARM_EVENT["detail"], "state": {"value": state}}}


# Estimador -----------------------------------------------------------------------------------


def test_estimator_should_record_snapshot_and_report_status(container):
    result = estimator.run(container.quality)
    assert result["prompt_version"] == "v1"
    assert result["status"] == "coletando"
    assert len(container.snapshots.recent(10)) == 1


def test_estimator_should_never_roll_back_even_when_breached(container):
    container.quality.deploy("v2")
    breach(container, "v2")

    first = estimator.run(container.quality)
    second = estimator.run(container.quality)

    assert first["status"] == second["status"] == "abaixo_da_meta"
    assert container.prompts.active().id == "v2"
    assert EventKind.ALARM in {event.kind for event in container.events.recent(10)}


def test_estimator_should_build_container_without_auto_rollback(monkeypatch):
    monkeypatch.setenv("PRUMO_AUTO_ROLLBACK", "true")
    monkeypatch.setenv("PRUMO_SIMULATION_WARMUP_HOURS", "0")
    estimator._container.cache_clear()
    try:
        assert estimator._container().quality.policy.auto_rollback is False
    finally:
        estimator._container.cache_clear()


def test_estimator_handler_should_bind_request_id(monkeypatch, container):
    monkeypatch.setattr(estimator, "_container", lambda: container)
    result = estimator.handler({}, SimpleNamespace(aws_request_id="req-42"))
    assert result["status"] == "coletando"
    assert correlation_id.get() == "req-42"


def test_bind_invocation_should_fall_back_when_context_has_no_request_id():
    assert bind_invocation(object()) == "-"


# Rollback ------------------------------------------------------------------------------------

HOUR = timedelta(hours=1)


def measure(container: Container, readings: int = 2) -> None:
    """Leituras horárias do estimador: são elas que o alarme do CloudWatch observa."""
    for _ in range(readings):
        container.clock.advance(HOUR)
        estimator.run(container.quality)


def breached_on(container: Container, *versions: str) -> None:
    """Publica as versões em sequência e deixa a última com 2 leituras abaixo da meta."""
    for version in versions:
        container.clock.advance(HOUR)
        container.quality.deploy(version)
    breach(container, versions[-1])
    measure(container)


def rollbacks(container: Container) -> list:
    return [e for e in container.events.recent(100) if e.kind is EventKind.ROLLBACK]


@pytest.mark.parametrize("state", ["OK", "INSUFFICIENT_DATA"])
def test_rollback_should_ignore_non_alarm_states(container, state):
    breached_on(container, "v2")
    result = rollback.handle_alarm(alarm(state), container)
    assert result["action"] == "ignored"
    assert container.prompts.active().id == "v2"


def test_rollback_should_ignore_other_event_types(container):
    event = {**alarm(), "detail-type": "EC2 Instance State-change Notification"}
    assert rollback.handle_alarm(event, container)["action"] == "ignored"


def test_rollback_should_restore_previous_version_on_alarm(container):
    breached_on(container, "v2")

    result = rollback.handle_alarm(alarm(), container)

    assert result == {"action": "rolled_back", "from": "v2", "to": "v1"}
    assert container.prompts.active().id == "v1"
    last = container.events.recent(1)[0]
    assert last.kind is EventKind.ROLLBACK
    assert last.details["reason"] == "cloudwatch_alarm"


def test_should_not_roll_back_twice_when_alarm_event_is_redelivered(container):
    breached_on(container, "v2", "v3")

    first = rollback.handle_alarm(alarm(), container)
    repeated = rollback.handle_alarm(alarm(), container)

    assert first == {"action": "rolled_back", "from": "v3", "to": "v2"}
    assert repeated == {"action": "skipped", "reason": "not_consecutive_for_active_version"}
    assert container.prompts.active().id == "v2"
    assert len(rollbacks(container)) == 1


def test_alarm_and_estimator_together_should_roll_back_only_once(container):
    breached_on(container, "v2", "v3")

    by_alarm = rollback.handle_alarm(alarm(), container)
    by_estimator = container.rollback_guard.attempt("estimator")

    assert by_alarm["action"] == "rolled_back"
    assert by_estimator.action == "skipped"
    assert container.prompts.active().id == "v2"
    assert len(rollbacks(container)) == 1


def test_should_not_roll_back_when_active_changed_since_check(container, monkeypatch):
    breached_on(container, "v2", "v3")
    original = container.prompts.rollback

    def someone_else_reverts_first(expected_active=None):
        original()  # um clique manual (v3 -> v2) chega entre a checagem e a troca
        return original(expected_active=expected_active)

    monkeypatch.setattr(container.prompts, "rollback", someone_else_reverts_first)

    result = rollback.handle_alarm(alarm(), container)

    assert result == {"action": "skipped", "reason": "rollback_conflict"}
    assert container.prompts.active().id == "v2"  # não voltou duas versões (v3 -> v1)
    assert rollbacks(container) == []


def test_should_compare_against_the_measured_version_not_a_fresh_read(container, monkeypatch):
    breached_on(container, "v2", "v3")
    guard = container.rollback_guard
    measure_breach = guard.breached_version

    def reverted_right_after_measuring() -> str | None:
        measured = measure_breach()
        container.prompts.rollback()  # clique manual (v3 -> v2) depois da medição
        return measured

    monkeypatch.setattr(guard, "breached_version", reverted_right_after_measuring)

    result = rollback.handle_alarm(alarm(), container)

    # Reler a versão ativa na hora da troca reverteria v2 -> v1 com base nas leituras da v3.
    assert result == {"action": "skipped", "reason": "rollback_conflict"}
    assert container.prompts.active().id == "v2"
    assert rollbacks(container) == []


def test_should_skip_with_a_single_breached_reading(container):
    container.quality.deploy("v2")
    breach(container, "v2")
    measure(container, readings=1)  # a regra pede 2 leituras seguidas; só há 1

    result = rollback.handle_alarm(alarm(), container)

    assert result == {"action": "skipped", "reason": "not_consecutive_for_active_version"}
    assert container.prompts.active().id == "v2"


def test_should_ignore_readings_from_before_the_last_version_change(container):
    breached_on(container, "v2")
    container.clock.advance(HOUR)
    container.quality.deploy("v3")
    container.clock.advance(HOUR)
    container.quality.rollback(reason="manual")  # v2 volta ao ar, mas as leituras são antigas

    result = rollback.handle_alarm(alarm(), container)

    assert result["reason"] == "not_consecutive_for_active_version"
    assert container.prompts.active().id == "v2"


def test_should_require_every_recent_reading_to_be_below_target(container):
    container.quality.deploy("v2")
    measure(container, readings=1)  # "coletando": ainda sem rótulos
    breach(container, "v2")
    measure(container, readings=1)

    result = rollback.handle_alarm(alarm(), container)

    assert result["reason"] == "not_consecutive_for_active_version"
    assert container.prompts.active().id == "v2"


def test_rollback_should_log_and_return_when_there_is_no_previous_version(container, caplog):
    breach(container, "v1")
    measure(container)
    with caplog.at_level(logging.WARNING):
        result = rollback.handle_alarm(alarm(), container)
    assert result == {"action": "skipped", "reason": "no_previous_version"}
    assert "rollback_skipped_no_previous_version" in caplog.messages


def test_rollback_should_respect_the_global_switch(container):
    breached_on(container, "v2")
    container.rollback_guard.enabled = False

    result = rollback.handle_alarm(alarm(), container)

    assert result == {"action": "disabled"}
    assert container.prompts.active().id == "v2"


def test_rollback_handler_should_use_cached_container(monkeypatch, container):
    breached_on(container, "v2")
    monkeypatch.setattr(rollback, "_container", lambda: container)
    result = rollback.handler(alarm(), SimpleNamespace(aws_request_id="req-7"))
    assert result["to"] == "v1"


def test_estimator_with_guard_should_roll_back_after_consecutive_readings(container):
    container.quality.deploy("v2")
    breach(container, "v2")
    container.clock.advance(HOUR)
    first = estimator.run(container.quality, container.rollback_guard)
    container.clock.advance(HOUR)
    second = estimator.run(container.quality, container.rollback_guard)

    assert first["rollback"]["action"] == "skipped"
    assert second["rollback"] == {"action": "rolled_back", "from": "v2", "to": "v1"}
    assert container.prompts.active().id == "v1"


# Tarefa de revisão (Step Functions) ------------------------------------------------------------


class FakeDecisions:
    def __init__(self, decision: Decision | None) -> None:
        self.decision = decision
        self.tokens: dict[str, str] = {}

    def get(self, decision_id: str) -> Decision | None:
        return self.decision

    def save_task_token(self, decision_id: str, task_token: str) -> None:
        if self.decision is None:
            raise NotFoundError("decisão não encontrada")
        self.tokens[decision_id] = task_token


PENDING = Decision(
    id="d-1",
    lead_text="lead",
    prompt_version="v1",
    predicted=LeadClass.FRIO,
    judge_approved=True,
    sampled_for_review=True,
    created_at=START,
)


def test_review_task_should_store_token_and_wait():
    store = FakeDecisions(PENDING)
    completed: list[tuple[str, Decision]] = []

    result = review_task.handle_review_task(
        {"decision_id": "d-1", "task_token": "tok-1"},
        store,
        lambda token, decision: completed.append((token, decision)),
    )

    assert result == {"decision_id": "d-1", "status": "waiting"}
    assert store.tokens == {"d-1": "tok-1"}
    assert completed == []


def test_review_task_should_complete_at_once_when_already_labeled():
    labeled = PENDING.model_copy(update={"human_correct": True, "reviewer": "ana"})
    completed: list[tuple[str, Decision]] = []

    result = review_task.handle_review_task(
        {"decision_id": "d-1", "task_token": "tok-1"},
        FakeDecisions(labeled),
        lambda token, decision: completed.append((token, decision)),
    )

    assert result["status"] == "completed"
    assert completed == [("tok-1", labeled)]


def test_review_task_should_reject_invalid_event_without_echoing_the_token():
    with pytest.raises(InvalidInputError) as caught:
        review_task.handle_review_task(
            {"decision_id": "", "task_token": "segredo-do-token"},
            FakeDecisions(PENDING),
            lambda token, decision: None,
        )
    assert caught.value.details == {"fields": ["decision_id"]}
    assert "segredo-do-token" not in str(caught.value)


def test_review_task_should_fail_when_decision_does_not_exist():
    with pytest.raises(NotFoundError):
        review_task.handle_review_task(
            {"decision_id": "d-9", "task_token": "tok"}, FakeDecisions(None), lambda t, d: None
        )


def test_review_task_handler_should_send_task_success_through_the_client(monkeypatch):
    sent: list[dict] = []
    client = SimpleNamespace(send_task_success=lambda **kwargs: sent.append(kwargs))
    labeled = PENDING.model_copy(update={"human_correct": False, "reviewer": "ana"})
    monkeypatch.setattr(review_task, "_dependencies", lambda: (FakeDecisions(labeled), client))

    review_task.handler({"decision_id": "d-1", "task_token": "tok-1"}, SimpleNamespace())

    assert sent[0]["taskToken"] == "tok-1"
    assert json.loads(sent[0]["output"])["human_correct"] is False


# API -----------------------------------------------------------------------------------------


def http_event(path: str) -> dict:
    return {
        "version": "2.0",
        "routeKey": "$default",
        "rawPath": path,
        "rawQueryString": "",
        "headers": {"host": "abc.execute-api.us-east-1.amazonaws.com"},
        "requestContext": {
            "http": {
                "method": "GET",
                "path": path,
                "protocol": "HTTP/1.1",
                "sourceIp": "203.0.113.10",
                "userAgent": "pytest",
            },
            "stage": "$default",
        },
        "isBase64Encoded": False,
    }


def test_api_handler_should_serve_requests_with_state_started_once(monkeypatch):
    monkeypatch.setenv("PRUMO_SIMULATION_WARMUP_HOURS", "0")
    sys.modules.pop("prumo.lambdas.api", None)
    try:
        api = importlib.import_module("prumo.lambdas.api")
        first = api.handler(http_event("/health"), SimpleNamespace())
        state = api.app.state.prumo
        second = api.handler(http_event("/health"), SimpleNamespace())
    finally:
        module = sys.modules.pop("prumo.lambdas.api", None)
        if module is not None:
            module._loop.close()
            asyncio.set_event_loop(None)

    assert first["statusCode"] == second["statusCode"] == 200
    assert json.loads(first["body"])["status"] == "ok"
    assert api.app.state.prumo is state

import itertools
import random
from datetime import UTC, datetime

import pytest

from prumo.adapters.memory import (
    InMemoryDecisionRepository,
    InMemoryEventLog,
    InMemoryPromptStore,
    InMemorySnapshotStore,
    ManualClock,
    NoopReviewWorkflow,
)
from prumo.domain.errors import ConflictError, InvalidInputError, NotFoundError, ProviderError
from prumo.domain.models import Classification, EventKind, LeadClass, PromptVersion, Verdict
from prumo.observability import NullMetricsPublisher
from prumo.prompts.catalog import PROMPTS
from prumo.services.decisions import BernoulliSampler, DecisionService, ReviewService
from prumo.services.quality import QualityPolicy, QualityService
from prumo.stats.slo import Slo


class FixedClassifier:
    def classify(self, lead_text: str, prompt: PromptVersion) -> Classification:
        return Classification(predicted=LeadClass.MORNO, rationale="fixo")


class FixedJudge:
    def __init__(self, approved: bool = True) -> None:
        self.approved = approved

    def judge(self, lead_text: str, classification: Classification) -> Verdict:
        return Verdict(approved=self.approved)


@pytest.fixture
def world():
    clock = ManualClock(datetime(2026, 10, 1, tzinfo=UTC))
    repo = InMemoryDecisionRepository()
    prompts = InMemoryPromptStore(PROMPTS, "v1")
    events = InMemoryEventLog()
    counter = itertools.count()
    new_id = lambda: f"id-{next(counter)}"  # noqa: E731
    workflow = NoopReviewWorkflow()
    decisions = DecisionService(
        classifier=FixedClassifier(),
        judge=FixedJudge(),
        repository=repo,
        prompts=prompts,
        review_workflow=workflow,
        sampler=lambda: True,
        clock=clock,
        new_id=new_id,
    )
    reviews = ReviewService(repository=repo, review_workflow=workflow, clock=clock)
    quality = QualityService(
        repository=repo,
        prompts=prompts,
        policy=QualityPolicy(slo=Slo(target=0.85, min_labels=5, breaches_to_rollback=2)),
        metrics=NullMetricsPublisher(),
        events=events,
        snapshots=InMemorySnapshotStore(),
        clock=clock,
        new_id=new_id,
    )
    return {
        "decisions": decisions,
        "reviews": reviews,
        "quality": quality,
        "repo": repo,
        "prompts": prompts,
        "events": events,
    }


def test_process_should_store_decision_with_active_prompt_version(world):
    decision = world["decisions"].process("  um lead qualquer  ")
    assert decision.prompt_version == "v1"
    assert decision.lead_text == "um lead qualquer"
    assert world["repo"].get(decision.id) == decision
    assert world["reviews"].pending() == [decision]


def test_process_should_reject_empty_lead(world):
    with pytest.raises(InvalidInputError):
        world["decisions"].process("   ")


def test_submit_should_label_once_and_leave_the_queue(world):
    decision = world["decisions"].process("lead")
    labeled = world["reviews"].submit(decision.id, correct=False, reviewer="ana")
    assert labeled.human_correct is False
    assert world["reviews"].pending() == []
    with pytest.raises(ConflictError):
        world["reviews"].submit(decision.id, correct=True, reviewer="ana")


def test_submit_should_fail_for_unknown_decision(world):
    with pytest.raises(NotFoundError):
        world["reviews"].submit("nao-existe", correct=True, reviewer="ana")


def test_quality_should_roll_back_after_two_confident_breaches(world):
    # Juiz aprova tudo, mas os humanos dizem que só metade está certa: bem abaixo de 85%.
    world["quality"].deploy("v2")
    for _ in range(2):
        for i in range(60):
            d = world["decisions"].process(f"lead {i}")
            world["reviews"].submit(d.id, correct=i % 2 == 0, reviewer="ana")
    first = world["quality"].evaluate()
    assert first.rolled_back_to is None
    second = world["quality"].evaluate()
    assert second.rolled_back_to is not None
    assert second.rolled_back_to.id == "v1"
    kinds = [e.kind for e in world["events"].recent(10)]
    assert kinds[:3] == [EventKind.ROLLBACK, EventKind.ALARM, EventKind.DEPLOY]


def test_deploy_should_reject_unknown_and_current_version(world):
    with pytest.raises(NotFoundError):
        world["quality"].deploy("v9")
    with pytest.raises(ConflictError):
        world["quality"].deploy("v1")


def test_rollback_should_fail_without_previous_version(world):
    with pytest.raises(ConflictError):
        world["quality"].rollback(reason="manual")


def test_sampler_should_respect_rate_bounds():
    sampler = BernoulliSampler(random.Random(1), 0.0)
    assert not any(sampler() for _ in range(100))
    with pytest.raises(InvalidInputError):
        sampler.rate = 1.5


def test_rollback_should_refuse_when_active_version_changed(world):
    world["quality"].deploy("v2")
    world["quality"].deploy("v3")
    with pytest.raises(ConflictError):
        world["quality"].rollback(reason="alarme", expected_active="v2")
    assert world["prompts"].active().id == "v3"


def test_resubmitting_the_same_label_should_be_idempotent(world):
    decision = world["decisions"].process("lead")
    first = world["reviews"].submit(decision.id, correct=True, reviewer="ana")
    again = world["reviews"].submit(decision.id, correct=True, reviewer="ana")
    assert again == first
    assert world["repo"].stats_for("v1").lab_n == 1


class FailingWorkflow:
    def request_review(self, decision):
        raise ProviderError("step functions fora")

    def complete_review(self, decision):
        raise ProviderError("step functions fora")


def test_workflow_failure_should_not_fail_a_saved_decision(world, caplog):
    service = DecisionService(
        classifier=FixedClassifier(),
        judge=FixedJudge(),
        repository=world["repo"],
        prompts=world["prompts"],
        review_workflow=FailingWorkflow(),
        sampler=lambda: True,
        clock=ManualClock(datetime(2026, 10, 1, tzinfo=UTC)),
        new_id=lambda: "fixo",
    )
    decision = service.process("lead")
    assert world["repo"].get(decision.id) is not None
    assert "review_workflow_failed" in caplog.text


def test_sampling_outside_the_simulator_should_use_the_os_random_source():
    import random as stdlib_random

    from prumo.config import Settings
    from prumo.container import _sampling_rng

    assert isinstance(_sampling_rng(Settings(), simulated=False), stdlib_random.SystemRandom)
    seeded = _sampling_rng(Settings(seed=1), simulated=True)
    assert not isinstance(seeded, stdlib_random.SystemRandom)


def test_admin_token_should_be_read_from_secrets_manager(monkeypatch):
    from prumo.adapters.aws import secrets
    from prumo.config import Settings
    from prumo.container import _resolve_admin_token

    calls = []
    monkeypatch.setattr(
        secrets, "read_secret", lambda arn, settings: calls.append(arn) or "s3gr3d0"
    )
    arn = "arn:aws:secretsmanager:x"
    token = _resolve_admin_token(Settings(admin_token_secret_arn=arn))
    assert token is not None
    assert token.get_secret_value() == "s3gr3d0"
    assert calls == ["arn:aws:secretsmanager:x"]

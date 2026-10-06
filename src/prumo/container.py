"""Montagem das dependências (composition root).

Escolhe as implementações das portas conforme a configuração. Os adaptadores da AWS são
importados só quando usados, para o modo local rodar sem credenciais.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import UTC, datetime

from pydantic import SecretStr

from prumo.adapters.memory import (
    InMemoryDecisionRepository,
    InMemoryEventLog,
    InMemoryPromptStore,
    InMemorySnapshotStore,
    ManualClock,
    NoopReviewWorkflow,
    SystemClock,
)
from prumo.adapters.simulated import (
    DEFAULT_PROFILES,
    SimulatedClassifier,
    SimulatedJudge,
    SimulatedReviewer,
)
from prumo.config import PromptBackend, Provider, Settings, Storage
from prumo.domain.ids import uuid7
from prumo.observability import EmfMetricsPublisher, NullMetricsPublisher
from prumo.ports import (
    Classifier,
    Clock,
    DecisionRepository,
    EventLog,
    Judge,
    MetricsPublisher,
    PromptStore,
    ReviewWorkflow,
    SnapshotStore,
)
from prumo.prompts.catalog import DEFAULT_VERSION, PROMPTS
from prumo.services.decisions import BernoulliSampler, DecisionService, ReviewService
from prumo.services.quality import QualityPolicy, QualityService
from prumo.services.rollback_guard import RollbackGuard
from prumo.stats.slo import Slo

SIMULATION_START = datetime(2026, 10, 1, tzinfo=UTC)


@dataclass
class Container:
    settings: Settings
    clock: Clock
    repository: DecisionRepository
    prompts: PromptStore
    events: EventLog
    snapshots: SnapshotStore
    sampler: BernoulliSampler
    decisions: DecisionService
    reviews: ReviewService
    quality: QualityService
    reviewer: SimulatedReviewer | None = None
    rng: random.Random = field(default_factory=random.Random)
    admin_token: SecretStr | None = None
    rollback_guard: RollbackGuard = field(init=False)

    def __post_init__(self) -> None:
        self.rollback_guard = RollbackGuard(
            quality=self.quality,
            prompts=self.prompts,
            snapshots=self.snapshots,
            events=self.events,
            enabled=self.settings.auto_rollback,
        )

    @property
    def is_simulated(self) -> bool:
        return self.settings.provider is Provider.SIMULATED


def build_container(settings: Settings | None = None) -> Container:
    settings = settings or Settings()
    simulated = settings.provider is Provider.SIMULATED
    # Semente fixa só serve para o simulador ser reprodutível; não tem papel de segurança.
    rng = random.Random(settings.seed)  # noqa: S311

    clock: Clock = ManualClock(SIMULATION_START) if simulated else SystemClock()
    prompts = _build_prompts(settings)
    repository = _build_repository(settings)
    review_workflow = _build_review_workflow(settings)
    classifier, judge = _build_models(settings, rng, prompts)
    events = _build_events(settings)
    snapshots = _build_snapshots(settings)
    metrics: MetricsPublisher = (
        NullMetricsPublisher() if simulated else EmfMetricsPublisher(settings.metrics_namespace)
    )
    sampler = BernoulliSampler(_sampling_rng(settings, simulated), settings.review_rate)

    policy = QualityPolicy(
        slo=Slo(
            target=settings.slo_target,
            min_labels=settings.min_labels,
            breaches_to_rollback=settings.breaches_to_rollback,
        ),
        alpha=settings.alpha,
        method=settings.method,
        auto_rollback=settings.auto_rollback,
    )
    return Container(
        settings=settings,
        clock=clock,
        repository=repository,
        prompts=prompts,
        events=events,
        snapshots=snapshots,
        sampler=sampler,
        decisions=DecisionService(
            classifier=classifier,
            judge=judge,
            repository=repository,
            prompts=prompts,
            review_workflow=review_workflow,
            sampler=sampler,
            clock=clock,
            new_id=uuid7,
        ),
        reviews=ReviewService(repository=repository, review_workflow=review_workflow, clock=clock),
        quality=QualityService(
            repository=repository,
            prompts=prompts,
            policy=policy,
            metrics=metrics,
            events=events,
            snapshots=snapshots,
            clock=clock,
            new_id=uuid7,
        ),
        reviewer=SimulatedReviewer(random.Random(settings.seed + 2)) if simulated else None,  # noqa: S311
        rng=rng,
        admin_token=_resolve_admin_token(settings),
    )


def _sampling_rng(settings: Settings, simulated: bool) -> random.Random:
    """Fora do simulador, o sorteio usa o gerador do sistema operacional.

    Com semente fixa, toda Lambda recém-iniciada sortearia as MESMAS posições (o 3º pedido,
    o 51º...), e a amostra deixaria de ser aleatória, enviesando a correção do juiz.
    """
    if simulated:
        return random.Random(settings.seed + 1)  # noqa: S311
    return random.SystemRandom()


def _resolve_admin_token(settings: Settings) -> SecretStr | None:
    if settings.admin_token is not None:
        return settings.admin_token
    if settings.admin_token_secret_arn:
        from prumo.adapters.aws.secrets import read_secret

        return SecretStr(read_secret(settings.admin_token_secret_arn, settings))
    return None


def _build_prompts(settings: Settings) -> PromptStore:
    if settings.prompt_backend is PromptBackend.APPCONFIG:
        from prumo.adapters.aws.appconfig import AppConfigPromptStore

        return AppConfigPromptStore.from_settings(settings, PROMPTS)
    return InMemoryPromptStore(PROMPTS, DEFAULT_VERSION)


def _build_repository(settings: Settings) -> DecisionRepository:
    if settings.storage is Storage.DYNAMODB:
        from prumo.adapters.aws.dynamodb import DynamoDecisionRepository

        return DynamoDecisionRepository.from_settings(settings)
    return InMemoryDecisionRepository()


def _build_events(settings: Settings) -> EventLog:
    if settings.storage is Storage.DYNAMODB:
        from prumo.adapters.aws.dynamodb import DynamoEventLog

        return DynamoEventLog.from_settings(settings)
    return InMemoryEventLog()


def _build_snapshots(settings: Settings) -> SnapshotStore:
    if settings.storage is Storage.DYNAMODB:
        from prumo.adapters.aws.dynamodb import DynamoSnapshotStore

        return DynamoSnapshotStore.from_settings(settings)
    return InMemorySnapshotStore()


def _build_review_workflow(settings: Settings) -> ReviewWorkflow:
    if settings.review_state_machine_arn:
        from prumo.adapters.aws.stepfunctions import StepFunctionsReviewWorkflow

        return StepFunctionsReviewWorkflow.from_settings(settings)
    return NoopReviewWorkflow()


def _build_models(
    settings: Settings, rng: random.Random, prompts: PromptStore
) -> tuple[Classifier, Judge]:
    if settings.provider is Provider.BEDROCK:
        from prumo.adapters.aws.bedrock import BedrockClassifier, BedrockJudge

        return BedrockClassifier.from_settings(settings), BedrockJudge.from_settings(settings)
    return (
        SimulatedClassifier(rng, DEFAULT_PROFILES),
        SimulatedJudge(rng, DEFAULT_PROFILES, lambda: prompts.active().id),
    )

"""Leitura de qualidade da versão em produção e decisão de rollback."""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass

from prumo.domain.errors import ConflictError
from prumo.domain.models import Event, EventKind, PromptVersion, QualitySnapshot
from prumo.ports import (
    Clock,
    DecisionRepository,
    EventLog,
    MetricsPublisher,
    PromptStore,
    SnapshotStore,
)
from prumo.stats.ppi import EstimationMethod, estimate
from prumo.stats.slo import Slo, SloStatus, should_rollback

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class QualityPolicy:
    slo: Slo
    alpha: float = 0.05
    method: EstimationMethod = EstimationMethod.PPI_CS
    planned_labels: int = 200
    auto_rollback: bool = True


@dataclass(frozen=True, slots=True)
class Evaluation:
    snapshot: QualitySnapshot
    rolled_back_to: PromptVersion | None = None


class QualityService:
    def __init__(
        self,
        *,
        repository: DecisionRepository,
        prompts: PromptStore,
        policy: QualityPolicy,
        metrics: MetricsPublisher,
        events: EventLog,
        snapshots: SnapshotStore,
        clock: Clock,
        new_id: Callable[[], str],
    ) -> None:
        self._repository = repository
        self._prompts = prompts
        self.policy = policy
        self._metrics = metrics
        self._events = events
        self._snapshots = snapshots
        self._clock = clock
        self._new_id = new_id
        self._history: dict[str, list[SloStatus]] = defaultdict(list)

    def read(self, prompt_version: str | None = None) -> QualitySnapshot:
        """Calcula a leitura atual sem efeitos colaterais."""
        version = prompt_version or self._prompts.active().id
        policy = self.policy
        stats = self._repository.stats_for(version)
        est = estimate(
            stats,
            alpha=policy.alpha,
            method=policy.method,
            planned_labels=policy.planned_labels,
        )
        status = policy.slo.status(est)
        return QualitySnapshot(
            at=self._clock.now(),
            prompt_version=version,
            status=status.value,
            method=policy.method.value,
            point=est.point if est else None,
            lower=est.lower if est else None,
            upper=est.upper if est else None,
            judge_rate=est.judge_rate if est else _judge_rate(stats.all_judge_sum, stats.all_n),
            burn_rate=policy.slo.burn_rate(est),
            n_labels=stats.lab_n,
            n_decisions=stats.all_n,
            slo_target=policy.slo.target,
        )

    def evaluate(self, *, truth: float | None = None) -> Evaluation:
        """Leitura periódica: publica métricas, registra o histórico e reverte se preciso."""
        snapshot = self.read()
        if truth is not None:
            snapshot = snapshot.model_copy(update={"truth": truth})
        # A métrica sai primeiro: ela decide o alarme, e não pode depender da gravação do
        # snapshot, que só serve ao painel.
        self._metrics.publish(snapshot)
        self._snapshots.append(snapshot)

        history = self._history[snapshot.prompt_version]
        history.append(SloStatus(snapshot.status))
        if not should_rollback(history, self.policy.slo):
            return Evaluation(snapshot=snapshot)

        self._record(
            EventKind.ALARM,
            f"{snapshot.prompt_version} abaixo da meta com segurança em "
            f"{self.policy.slo.breaches_to_rollback} leituras seguidas.",
            {"upper": snapshot.upper, "target": self.policy.slo.target},
        )
        if not self.policy.auto_rollback:
            history.clear()
            return Evaluation(snapshot=snapshot)
        restored = self.rollback(reason="slo", expected_active=snapshot.prompt_version)
        return Evaluation(snapshot=snapshot, rolled_back_to=restored)

    def deploy(self, version_id: str) -> PromptVersion:
        before = self._prompts.active().id
        deployed = self._prompts.deploy(version_id)
        self._history[deployed.id].clear()
        self._record(
            EventKind.DEPLOY,
            f"Prompt {deployed.id} publicado (antes: {before}).",
            {"from": before, "to": deployed.id},
        )
        return deployed

    def rollback(self, *, reason: str, expected_active: str | None = None) -> PromptVersion:
        current = expected_active or self._prompts.active().id
        if self._prompts.previous() is None:
            raise ConflictError("não há versão anterior para voltar")
        restored = self._prompts.rollback(expected_active=current)
        self._history[current].clear()
        self._history[restored.id].clear()
        self._record(
            EventKind.ROLLBACK,
            f"Rollback: {current} → {restored.id}.",
            {"from": current, "to": restored.id, "reason": reason},
        )
        logger.warning(
            "prompt_rollback",
            extra={"from_version": current, "to_version": restored.id, "reason": reason},
        )
        return restored

    def note(self, message: str) -> None:
        self._record(EventKind.INFO, message, {})

    def _record(self, kind: EventKind, message: str, details: dict[str, object]) -> None:
        self._events.append(
            Event(
                id=self._new_id(), at=self._clock.now(), kind=kind, message=message, details=details
            )
        )


def _judge_rate(total: float, n: int) -> float | None:
    return total / n if n else None

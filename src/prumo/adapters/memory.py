"""Implementações em memória das portas. Usadas no modo local e nos testes."""

from __future__ import annotations

import threading
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta

from prumo.domain.errors import ConflictError, NotFoundError
from prumo.domain.models import Decision, Event, PromptVersion, QualitySnapshot
from prumo.stats.ppi import SufficientStats

_MAX_EVENTS = 500
_MAX_SNAPSHOTS = 2_000


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class ManualClock:
    """Relógio controlado: o simulador avança uma hora por passo."""

    def __init__(self, start: datetime) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta


class InMemoryDecisionRepository:
    """Guarda decisões e mantém as somas por versão, para estimar em O(1)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._decisions: dict[str, Decision] = {}
        self._pending: dict[str, None] = {}  # dict ordenado = fila em ordem de chegada
        self._stats: dict[str, SufficientStats] = defaultdict(SufficientStats)

    def add(self, decision: Decision) -> None:
        with self._lock:
            if decision.id in self._decisions:
                raise ConflictError("decisão já existe", details={"id": decision.id})
            self._decisions[decision.id] = decision
            self._stats[decision.prompt_version] += SufficientStats.from_judged(
                float(decision.judge_approved)
            )
            if decision.is_pending_review:
                self._pending[decision.id] = None

    def get(self, decision_id: str) -> Decision | None:
        return self._decisions.get(decision_id)

    def save_label(self, decision: Decision) -> None:
        with self._lock:
            current = self._decisions.get(decision.id)
            if current is None:
                raise NotFoundError("decisão não encontrada", details={"id": decision.id})
            if current.human_correct is not None:
                raise ConflictError("decisão já rotulada", details={"id": decision.id})
            if decision.human_correct is None:
                raise ConflictError("rótulo ausente", details={"id": decision.id})
            self._decisions[decision.id] = decision
            self._pending.pop(decision.id, None)
            self._stats[decision.prompt_version] += SufficientStats.from_label(
                float(decision.human_correct), float(decision.judge_approved)
            )

    def pending_review(self, limit: int) -> list[Decision]:
        ids = list(self._pending)[-limit:] if limit > 0 else []
        return [self._decisions[i] for i in reversed(ids)]

    def stats_for(self, prompt_version: str) -> SufficientStats:
        return self._stats.get(prompt_version, SufficientStats())

    def discard_pending_older_than(self, cutoff: datetime) -> int:
        """Tira da fila itens não revisados antigos (a amostra continua aleatória)."""
        with self._lock:
            stale = [i for i in self._pending if self._decisions[i].created_at < cutoff]
            for decision_id in stale:
                self._pending.pop(decision_id, None)
            return len(stale)


class InMemoryPromptStore:
    def __init__(self, catalog: tuple[PromptVersion, ...], initial: str) -> None:
        self._catalog = {p.id: p for p in catalog}
        if initial not in self._catalog:
            raise NotFoundError("versão inicial inexistente", details={"id": initial})
        self._history: list[str] = [initial]

    def catalog(self) -> list[PromptVersion]:
        return list(self._catalog.values())

    def active(self) -> PromptVersion:
        return self._catalog[self._history[-1]]

    def deploy(self, version_id: str) -> PromptVersion:
        if version_id not in self._catalog:
            raise NotFoundError("versão de prompt inexistente", details={"id": version_id})
        if version_id == self._history[-1]:
            raise ConflictError("essa versão já está em produção", details={"id": version_id})
        self._history.append(version_id)
        return self.active()

    def previous(self) -> PromptVersion | None:
        return self._catalog[self._history[-2]] if len(self._history) > 1 else None

    def rollback(self, expected_active: str | None = None) -> PromptVersion:
        if expected_active is not None and expected_active != self._history[-1]:
            raise ConflictError(
                "a versão no ar mudou desde a leitura; rollback cancelado",
                details={"expected": expected_active, "active": self._history[-1]},
            )
        if len(self._history) < 2:
            raise ConflictError("não há versão anterior para voltar")
        self._history.pop()
        return self.active()


class InMemoryEventLog:
    def __init__(self) -> None:
        self._events: deque[Event] = deque(maxlen=_MAX_EVENTS)

    def append(self, event: Event) -> None:
        self._events.append(event)

    def recent(self, limit: int) -> list[Event]:
        return list(reversed(self._events))[:limit]


class InMemorySnapshotStore:
    def __init__(self) -> None:
        self._items: deque[QualitySnapshot] = deque(maxlen=_MAX_SNAPSHOTS)

    def append(self, snapshot: QualitySnapshot) -> None:
        self._items.append(snapshot)

    def recent(self, limit: int) -> list[QualitySnapshot]:
        return list(self._items)[-limit:]


class NoopReviewWorkflow:
    """No modo local, a fila de revisão é só o repositório."""

    def request_review(self, decision: Decision) -> None:
        return None

    def complete_review(self, decision: Decision) -> None:
        return None

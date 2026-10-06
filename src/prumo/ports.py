"""Portas (interfaces) do Prumo. Os serviços dependem delas, nunca de boto3 diretamente.

Cada porta tem uma implementação local (memória/simulada, para rodar sem AWS) e uma na AWS.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from prumo.domain.models import (
    Classification,
    Decision,
    Event,
    PromptVersion,
    QualitySnapshot,
    Verdict,
)
from prumo.stats.ppi import SufficientStats


class Clock(Protocol):
    def now(self) -> datetime: ...


class Classifier(Protocol):
    """A IA monitorada: classifica um lead com a versão de prompt informada."""

    def classify(self, lead_text: str, prompt: PromptVersion) -> Classification: ...


class Judge(Protocol):
    """O juiz LLM: diz se a classificação está correta segundo a rubrica."""

    def judge(self, lead_text: str, classification: Classification) -> Verdict: ...


class DecisionRepository(Protocol):
    def add(self, decision: Decision) -> None: ...

    def get(self, decision_id: str) -> Decision | None: ...

    def save_label(self, decision: Decision) -> None:
        """Grava o rótulo humano de uma decisão já existente."""
        ...

    def pending_review(self, limit: int) -> list[Decision]: ...

    def stats_for(self, prompt_version: str) -> SufficientStats: ...


class PromptStore(Protocol):
    def catalog(self) -> list[PromptVersion]: ...

    def active(self) -> PromptVersion: ...

    def deploy(self, version_id: str) -> PromptVersion:
        """Publica uma versão e lembra qual estava antes (para o rollback)."""
        ...

    def previous(self) -> PromptVersion | None: ...

    def rollback(self, expected_active: str | None = None) -> PromptVersion:
        """Volta para a versão anterior. Erro se não houver anterior.

        Com `expected_active`, só reverte se essa ainda for a versão no ar (compare-and-swap):
        um alarme entregue duas vezes, ou um clique manual junto com o alarme, nunca volta duas
        versões de uma vez. Se a versão mudou, lança ConflictError.
        """
        ...


class EventLog(Protocol):
    def append(self, event: Event) -> None: ...

    def recent(self, limit: int) -> list[Event]: ...


class SnapshotStore(Protocol):
    def append(self, snapshot: QualitySnapshot) -> None: ...

    def recent(self, limit: int) -> list[QualitySnapshot]: ...


class MetricsPublisher(Protocol):
    def publish(self, snapshot: QualitySnapshot) -> None: ...


class ReviewWorkflow(Protocol):
    """Avisa o fluxo de revisão humana (na AWS: Step Functions com waitForTaskToken)."""

    def request_review(self, decision: Decision) -> None: ...

    def complete_review(self, decision: Decision) -> None: ...

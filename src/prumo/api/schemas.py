"""Contratos da API (entrada validada e saída explícita, sem expor campos internos)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from prumo.domain.models import Decision, Event, LeadClass, PromptVersion, QualitySnapshot


class _Input(BaseModel):
    # Campos desconhecidos são recusados: protege contra atribuição em massa.
    model_config = ConfigDict(extra="forbid")


class DecisionIn(_Input):
    lead_text: str = Field(min_length=1, max_length=2000)


class ReviewIn(_Input):
    correct: bool
    reviewer: str = Field(default="painel", min_length=1, max_length=80)


class AdvanceIn(_Input):
    hours: int = Field(default=1, ge=1, le=48)


class SimulationSettingsIn(_Input):
    labels_per_hour: int | None = Field(default=None, ge=0, le=400)
    auto_rollback: bool | None = None


class DecisionOut(BaseModel):
    id: str
    lead_text: str
    prompt_version: str
    predicted: LeadClass
    rationale: str
    judge_approved: bool
    judge_reason: str
    sampled_for_review: bool
    human_correct: bool | None
    created_at: datetime
    expected: LeadClass | None = Field(
        default=None, description="Gabarito da rubrica. Só aparece no modo simulado, após rotular."
    )

    @classmethod
    def of(cls, decision: Decision, expected: LeadClass | None = None) -> DecisionOut:
        return cls(**decision.model_dump(exclude={"reviewer", "labeled_at"}), expected=expected)


class PromptOut(BaseModel):
    id: str
    title: str

    @classmethod
    def of(cls, prompt: PromptVersion) -> PromptOut:
        return cls(id=prompt.id, title=prompt.title)


class PromptsOut(BaseModel):
    active: PromptOut
    previous: PromptOut | None
    catalog: list[PromptOut]


class QualityOut(BaseModel):
    current: QualitySnapshot
    history: list[QualitySnapshot]


class EventsOut(BaseModel):
    items: list[Event]


class SimulationOut(BaseModel):
    enabled: bool
    clock: datetime | None = None
    labels_per_hour: int | None = None
    leads_per_hour: int | None = None
    auto_rollback: bool


class ErrorOut(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    correlation_id: str

"""Modelos do domínio: o que entra, o que a IA decide e o que o Prumo mede."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

PROMPT_VERSION_PATTERN = r"^v[0-9]{1,4}$"


class LeadClass(StrEnum):
    QUENTE = "QUENTE"
    MORNO = "MORNO"
    FRIO = "FRIO"


class Classification(BaseModel):
    """Saída da IA classificadora."""

    model_config = ConfigDict(frozen=True)

    predicted: LeadClass
    rationale: str = Field(default="", max_length=600)


class Verdict(BaseModel):
    """Saída do juiz LLM: a classificação está correta pela rubrica?"""

    model_config = ConfigDict(frozen=True)

    approved: bool
    reason: str = Field(default="", max_length=600)


class PromptVersion(BaseModel):
    """Uma versão do prompt da IA classificadora. É a unidade de deploy e de rollback."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(pattern=PROMPT_VERSION_PATTERN)
    title: str = Field(max_length=120)
    system_prompt: str = Field(min_length=20, max_length=8000)


class Decision(BaseModel):
    """Uma decisão da IA, com o veredito do juiz e, se sorteada, o rótulo humano."""

    id: str
    lead_text: str = Field(min_length=1, max_length=2000)
    prompt_version: str = Field(pattern=PROMPT_VERSION_PATTERN)
    predicted: LeadClass
    rationale: str = ""
    judge_approved: bool
    judge_reason: str = ""
    sampled_for_review: bool = False
    human_correct: bool | None = None
    reviewer: str | None = None
    labeled_at: datetime | None = None
    created_at: datetime

    @property
    def is_pending_review(self) -> bool:
        return self.sampled_for_review and self.human_correct is None


class EventKind(StrEnum):
    DEPLOY = "deploy"
    ROLLBACK = "rollback"
    ALARM = "alarm"
    INFO = "info"


class Event(BaseModel):
    """Trilha de auditoria: deploys, alarmes e rollbacks."""

    model_config = ConfigDict(frozen=True)

    id: str
    at: datetime
    kind: EventKind
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class QualitySnapshot(BaseModel):
    """Uma leitura do Prumo para a versão em produção."""

    model_config = ConfigDict(frozen=True)

    at: datetime
    prompt_version: str
    status: str
    method: str
    point: float | None = None
    lower: float | None = None
    upper: float | None = None
    judge_rate: float | None = None
    burn_rate: float | None = None
    n_labels: int = 0
    n_decisions: int = 0
    slo_target: float
    truth: float | None = Field(
        default=None, description="Só no simulador: a taxa real verdadeira, invisível em produção."
    )

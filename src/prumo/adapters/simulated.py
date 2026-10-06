"""IA classificadora, juiz e revisor humano simulados, para rodar sem AWS e sem custo.

Cada versão de prompt tem um perfil de erro. A v2 erra mais, e erra de um jeito que o juiz
deixa passar: é exatamente o caso que o Prumo existe para pegar.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass

from prumo.data.leads import expected_class_for
from prumo.domain.errors import InvalidInputError
from prumo.domain.models import Classification, LeadClass, PromptVersion, Verdict

_ADJACENT: dict[LeadClass, tuple[LeadClass, ...]] = {
    LeadClass.QUENTE: (LeadClass.MORNO,),
    LeadClass.MORNO: (LeadClass.QUENTE, LeadClass.FRIO),
    LeadClass.FRIO: (LeadClass.MORNO,),
}


@dataclass(frozen=True, slots=True)
class ErrorProfile:
    accuracy: float
    """Chance de a IA acertar a classe."""
    judge_miss: float
    """Chance de o juiz aprovar uma decisão errada (ponto cego)."""
    judge_hit: float = 0.97
    """Chance de o juiz aprovar uma decisão certa."""


DEFAULT_PROFILES: dict[str, ErrorProfile] = {
    "v1": ErrorProfile(accuracy=0.92, judge_miss=0.35),
    "v2": ErrorProfile(accuracy=0.76, judge_miss=0.72),
    "v3": ErrorProfile(accuracy=0.93, judge_miss=0.30),
}


def _truth(lead_text: str) -> LeadClass:
    expected = expected_class_for(lead_text)
    if expected is None:
        raise InvalidInputError(
            "o modo simulado só entende leads do gerador sintético; use o modo bedrock "
            "para textos livres",
            details={"lead_text": lead_text[:120]},
        )
    return expected


class SimulatedClassifier:
    def __init__(self, rng: random.Random, profiles: dict[str, ErrorProfile]) -> None:
        self._rng = rng
        self._profiles = profiles

    def classify(self, lead_text: str, prompt: PromptVersion) -> Classification:
        truth = _truth(lead_text)
        profile = self._profiles.get(prompt.id, DEFAULT_PROFILES["v1"])
        if self._rng.random() < profile.accuracy:
            return Classification(predicted=truth, rationale="rubrica aplicada")
        wrong = self._rng.choice(_ADJACENT[truth])
        return Classification(predicted=wrong, rationale="rubrica aplicada")


class SimulatedJudge:
    def __init__(
        self,
        rng: random.Random,
        profiles: dict[str, ErrorProfile],
        active_version: Callable[[], str],
    ) -> None:
        self._rng = rng
        self._profiles = profiles
        self._active_version = active_version

    def judge(self, lead_text: str, classification: Classification) -> Verdict:
        correct = classification.predicted == _truth(lead_text)
        profile = self._profiles.get(self._active_version(), DEFAULT_PROFILES["v1"])
        chance = profile.judge_hit if correct else profile.judge_miss
        approved = self._rng.random() < chance
        return Verdict(approved=approved, reason="conferido pela rubrica")


class SimulatedReviewer:
    """Revisor humano com uma pequena taxa de engano, como gente de verdade."""

    def __init__(self, rng: random.Random, noise: float = 0.01) -> None:
        if not 0 <= noise < 0.5:
            raise ValueError("noise precisa estar em [0, 0.5)")
        self._rng = rng
        self._noise = noise

    def review(self, lead_text: str, predicted: LeadClass) -> bool:
        correct = predicted == _truth(lead_text)
        return (not correct) if self._rng.random() < self._noise else correct

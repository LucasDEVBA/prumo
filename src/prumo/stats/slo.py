"""Meta de qualidade (SLO), orçamento de erro e a regra que decide reverter o prompt."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from prumo.stats.ppi import Estimate


class SloStatus(StrEnum):
    COLLECTING = "coletando"
    """Ainda não há rótulos suficientes para afirmar nada."""
    HEALTHY = "saudavel"
    """A faixa inteira está acima da meta."""
    AT_RISK = "em_observacao"
    """A meta está dentro da faixa: pode estar acima ou abaixo."""
    BREACHED = "abaixo_da_meta"
    """A faixa inteira está abaixo da meta: dá para afirmar que piorou."""


@dataclass(frozen=True, slots=True)
class Slo:
    target: float = 0.85
    min_labels: int = 30
    breaches_to_rollback: int = 2

    def __post_init__(self) -> None:
        if not 0 < self.target < 1:
            raise ValueError("a meta precisa estar entre 0 e 1")
        if self.min_labels < 2 or self.breaches_to_rollback < 1:
            raise ValueError("min_labels >= 2 e breaches_to_rollback >= 1")

    @property
    def error_budget(self) -> float:
        """Fração de decisões erradas que a meta tolera."""
        return 1 - self.target

    def status(self, est: Estimate | None) -> SloStatus:
        if est is None or est.n_labels < self.min_labels:
            return SloStatus.COLLECTING
        if est.upper < self.target:
            return SloStatus.BREACHED
        if est.lower >= self.target:
            return SloStatus.HEALTHY
        return SloStatus.AT_RISK

    def burn_rate(self, est: Estimate | None) -> float | None:
        """Velocidade de gasto do orçamento de erro: 1,0 = gasta exatamente o orçamento."""
        if est is None:
            return None
        return (1 - est.point) / self.error_budget


def should_rollback(history: list[SloStatus], slo: Slo) -> bool:
    """Reverte só depois de N avaliações seguidas abaixo da meta, contando a mais recente."""
    tail = history[-slo.breaches_to_rollback :]
    return len(tail) == slo.breaches_to_rollback and all(
        status is SloStatus.BREACHED for status in tail
    )

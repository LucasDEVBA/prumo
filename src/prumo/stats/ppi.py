"""Estimativa da taxa real de acerto a partir de um juiz LLM + uma amostra humana.

O juiz LLM avalia todas as decisões, mas erra de forma sistemática. Os rótulos humanos de
uma amostra medem esse viés (o "retificador") e o descontam da média do juiz. Essa é a
Prediction-Powered Inference (Angelopoulos et al., Science 2023). A variante PPI++
(Angelopoulos, Duchi & Zrnic, 2023) pondera o juiz por um fator lambda escolhido pelos dados,
o que garante nunca ser pior do que usar só os rótulos humanos.

Tudo aqui trabalha com estatísticas suficientes (somas), para que contagens horárias possam
ser agregadas no banco sem guardar cada item na memória.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum
from statistics import NormalDist

from prumo.stats.sequences import asymptotic_cs_radius

_MIN_LABELS = 2
_VARIANCE_FLOOR = 1e-9


class EstimationMethod(StrEnum):
    """Como transformar contagens em uma faixa para a taxa real."""

    PPI_CI = "ppi_ci"
    """PPI++ com intervalo de confiança clássico (válido para uma única consulta)."""

    PPI_CS = "ppi_cs"
    """PPI com sequência de confiança: válida mesmo consultada a cada hora."""

    HUMAN_ONLY = "human_only"
    """Baseline: só a amostra humana, ignorando o juiz."""


@dataclass(frozen=True, slots=True)
class SufficientStats:
    """Somas que bastam para estimar. Juiz e humano valem 1 (certo) ou 0 (errado).

    `all_*` cobre todas as decisões avaliadas pelo juiz (inclusive as rotuladas).
    `lab_*` cobre apenas as decisões que também receberam rótulo humano.
    """

    all_n: int = 0
    all_judge_sum: float = 0.0
    all_judge_sq_sum: float = 0.0
    lab_n: int = 0
    lab_human_sum: float = 0.0
    lab_human_sq_sum: float = 0.0
    lab_judge_sum: float = 0.0
    lab_judge_sq_sum: float = 0.0
    lab_cross_sum: float = 0.0

    def __add__(self, other: SufficientStats) -> SufficientStats:
        return SufficientStats(
            all_n=self.all_n + other.all_n,
            all_judge_sum=self.all_judge_sum + other.all_judge_sum,
            all_judge_sq_sum=self.all_judge_sq_sum + other.all_judge_sq_sum,
            lab_n=self.lab_n + other.lab_n,
            lab_human_sum=self.lab_human_sum + other.lab_human_sum,
            lab_human_sq_sum=self.lab_human_sq_sum + other.lab_human_sq_sum,
            lab_judge_sum=self.lab_judge_sum + other.lab_judge_sum,
            lab_judge_sq_sum=self.lab_judge_sq_sum + other.lab_judge_sq_sum,
            lab_cross_sum=self.lab_cross_sum + other.lab_cross_sum,
        )

    @classmethod
    def from_judged(cls, judge: float) -> SufficientStats:
        """Uma decisão avaliada só pelo juiz."""
        _check_unit(judge, "judge")
        return cls(all_n=1, all_judge_sum=judge, all_judge_sq_sum=judge * judge)

    @classmethod
    def from_label(cls, human: float, judge: float) -> SufficientStats:
        """Um rótulo humano para uma decisão que o juiz já avaliou (não reconta o juiz)."""
        _check_unit(human, "human")
        _check_unit(judge, "judge")
        return cls(
            lab_n=1,
            lab_human_sum=human,
            lab_human_sq_sum=human * human,
            lab_judge_sum=judge,
            lab_judge_sq_sum=judge * judge,
            lab_cross_sum=human * judge,
        )

    @classmethod
    def from_samples(
        cls, judge_all: list[float], labeled: list[tuple[float, float]]
    ) -> SufficientStats:
        """Monta as somas a partir de listas. `labeled` contém pares (humano, juiz)."""
        total = SufficientStats()
        for judge in judge_all:
            total += cls.from_judged(judge)
        for human, judge in labeled:
            total += cls.from_label(human, judge)
        return total


@dataclass(frozen=True, slots=True)
class Estimate:
    """Resultado: ponto estimado e faixa, sempre dentro de [0, 1]."""

    method: EstimationMethod
    point: float
    lower: float
    upper: float
    n_labels: int
    n_decisions: int
    judge_rate: float
    lam: float = field(default=1.0)

    @property
    def half_width(self) -> float:
        return (self.upper - self.lower) / 2


def estimate(
    stats: SufficientStats,
    *,
    alpha: float = 0.05,
    method: EstimationMethod = EstimationMethod.PPI_CS,
    planned_labels: int = 200,
    planning_variance: float = 0.15,
) -> Estimate | None:
    """Estima a taxa real de acerto. Devolve None enquanto não há rótulos suficientes."""
    if not 0 < alpha < 1:
        raise ValueError(f"alpha precisa estar entre 0 e 1, recebido {alpha}")
    if stats.lab_n < _MIN_LABELS or stats.all_n < stats.lab_n:
        return None

    match method:
        case EstimationMethod.HUMAN_ONLY:
            return _human_only(stats, alpha)
        case EstimationMethod.PPI_CI:
            return _ppi_plus_plus_ci(stats, alpha)
        case EstimationMethod.PPI_CS:
            return _ppi_cs(stats, alpha, planned_labels, planning_variance)


def _human_only(s: SufficientStats, alpha: float) -> Estimate:
    mean_h = s.lab_human_sum / s.lab_n
    var_h = _sample_variance(s.lab_human_sum, s.lab_human_sq_sum, s.lab_n)
    radius = _z(alpha) * math.sqrt(var_h / s.lab_n)
    return _build(EstimationMethod.HUMAN_ONLY, mean_h, radius, s, lam=0.0)


def _ppi_plus_plus_ci(s: SufficientStats, alpha: float) -> Estimate:
    n, big_n = s.lab_n, s.all_n
    mean_j_all = s.all_judge_sum / big_n
    var_j_all = _sample_variance(s.all_judge_sum, s.all_judge_sq_sum, big_n)
    mean_h = s.lab_human_sum / n
    mean_j_lab = s.lab_judge_sum / n
    var_h = _sample_variance(s.lab_human_sum, s.lab_human_sq_sum, n)
    var_j_lab = _sample_variance(s.lab_judge_sum, s.lab_judge_sq_sum, n)
    cov_hj = (s.lab_cross_sum - n * mean_h * mean_j_lab) / (n - 1)

    # lambda ótimo do PPI++: pesa o juiz conforme ele se correlaciona com o humano.
    denom = (1 + n / big_n) * var_j_all
    lam = 0.0 if denom <= _VARIANCE_FLOOR else min(1.0, max(0.0, cov_hj / denom))

    point = lam * mean_j_all + (mean_h - lam * mean_j_lab)
    var_rect = max(var_h + lam * lam * var_j_lab - 2 * lam * cov_hj, _VARIANCE_FLOOR)
    variance = lam * lam * var_j_all / big_n + var_rect / n
    radius = _z(alpha) * math.sqrt(variance)
    return _build(EstimationMethod.PPI_CI, point, radius, s, lam=lam)


def _ppi_cs(
    s: SufficientStats, alpha: float, planned_labels: int, planning_variance: float
) -> Estimate:
    """PPI clássico (lambda = 1) com sequência de confiança.

    O lambda fica fixo em 1 porque escolhê-lo pelos dados a cada consulta quebraria a garantia
    de validade a qualquer momento. O alpha é dividido entre o termo do juiz e o retificador
    (desigualdade da união), e os dois termos usam a fronteira de mistura gaussiana.
    """
    n, big_n = s.lab_n, s.all_n
    mean_j_all = s.all_judge_sum / big_n
    var_j_all = _sample_variance(s.all_judge_sum, s.all_judge_sq_sum, big_n)
    rect_sum = s.lab_human_sum - s.lab_judge_sum
    rect_sq_sum = s.lab_human_sq_sum - 2 * s.lab_cross_sum + s.lab_judge_sq_sum
    mean_rect = rect_sum / n
    var_rect = _sample_variance(rect_sum, rect_sq_sum, n)

    half_alpha = alpha / 2
    radius = asymptotic_cs_radius(
        n,
        var_rect,
        half_alpha,
        planned_samples=planned_labels,
        planning_variance=planning_variance,
    ) + asymptotic_cs_radius(
        big_n,
        var_j_all,
        half_alpha,
        planned_samples=max(big_n, planned_labels),
        planning_variance=0.25,
    )
    return _build(EstimationMethod.PPI_CS, mean_j_all + mean_rect, radius, s, lam=1.0)


def _build(
    method: EstimationMethod, point: float, radius: float, s: SufficientStats, *, lam: float
) -> Estimate:
    clipped = min(1.0, max(0.0, point))
    return Estimate(
        method=method,
        point=clipped,
        lower=max(0.0, point - radius),
        upper=min(1.0, point + radius),
        n_labels=s.lab_n,
        n_decisions=s.all_n,
        judge_rate=s.all_judge_sum / s.all_n if s.all_n else 0.0,
        lam=lam,
    )


def _sample_variance(total: float, sq_total: float, n: int) -> float:
    if n < _MIN_LABELS:
        return 0.0
    mean = total / n
    return max((sq_total - n * mean * mean) / (n - 1), _VARIANCE_FLOOR)


def _z(alpha: float) -> float:
    return NormalDist().inv_cdf(1 - alpha / 2)


def _check_unit(value: float, name: str) -> None:
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} precisa estar em [0, 1], recebido {value}")

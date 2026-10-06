"""Sequências de confiança: faixas que continuam válidas mesmo consultadas a cada hora.

Um intervalo de confiança comum vale para UMA olhada. Se o painel recalcula a cada hora e o
alarme dispara na primeira vez que a faixa cruza a meta, a chance de alarme falso cresce a cada
consulta. A sequência de confiança assintótica de Waudby-Smith et al. (Annals of Statistics,
2024) usa a fronteira de mistura gaussiana de Robbins e garante cobertura uniforme no tempo.
"""

from __future__ import annotations

import math

_PLANNING_FLOOR = 1e-6


def optimal_rho_squared(alpha: float, planned_samples: int, planning_variance: float) -> float:
    """Escolhe o parâmetro da mistura para a faixa ficar mais estreita perto de `planned_samples`.

    Qualquer valor positivo mantém a validade; este só controla onde a faixa é mais justa.
    """
    if planned_samples < 1:
        raise ValueError("planned_samples precisa ser positivo")
    log_term = -2 * math.log(alpha)
    numerator = log_term + math.log(log_term + 1)
    return numerator / (planned_samples * max(planning_variance, _PLANNING_FLOOR))


def asymptotic_cs_radius(
    n: int,
    variance: float,
    alpha: float,
    *,
    planned_samples: int = 200,
    planning_variance: float = 0.15,
) -> float:
    """Meia-largura da sequência de confiança bilateral depois de `n` observações."""
    if n < 1:
        return math.inf
    if not 0 < alpha < 1:
        raise ValueError(f"alpha precisa estar entre 0 e 1, recebido {alpha}")
    rho2 = optimal_rho_squared(alpha, planned_samples, planning_variance)
    scaled = n * max(variance, 0.0) * rho2 + 1
    return math.sqrt(2 * scaled / (n * n * rho2) * math.log(math.sqrt(scaled) / alpha))

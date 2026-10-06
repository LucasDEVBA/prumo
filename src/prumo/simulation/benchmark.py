"""Benchmark: quanto tempo o Prumo leva para pegar um prompt ruim, e quantas vezes erra.

Para cada método e quantidade de rótulos por hora, roda várias sementes:
1. aquece com a v1;
2. publica a v2 (pior) e mede em quantas horas acontece o rollback;
3. publica a v3 (melhor) e conta rollbacks indevidos.
O "juiz sozinho" serve de comparação: ele nunca alarma, porque a nota dele mal se mexe.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

from prumo.config import Settings
from prumo.container import build_container
from prumo.simulation.engine import Simulator
from prumo.stats.ppi import EstimationMethod

MAX_HOURS = 72


@dataclass(frozen=True, slots=True)
class BenchRow:
    method: EstimationMethod
    labels_per_hour: int
    detection_hours: list[int | None]
    false_rollbacks: int
    runs: int

    @property
    def detected(self) -> list[int]:
        return [h for h in self.detection_hours if h is not None]

    def median_hours(self) -> float | None:
        return statistics.median(self.detected) if self.detected else None


def run_case(method: EstimationMethod, labels_per_hour: int, seed: int) -> tuple[int | None, bool]:
    container = build_container(Settings(seed=seed, method=method))
    sim = Simulator(container, labels_per_hour=labels_per_hour)
    sim.advance(24)
    container.quality.deploy("v2")
    detected = next(
        (hour for hour, r in enumerate(sim.advance(MAX_HOURS), start=1) if r.rolled_back_to),
        None,
    )
    container.quality.deploy("v3")
    false_alarm = any(r.rolled_back_to for r in sim.advance(MAX_HOURS))
    return detected, false_alarm


def run(
    methods: tuple[EstimationMethod, ...] = (EstimationMethod.PPI_CS, EstimationMethod.PPI_CI),
    labels: tuple[int, ...] = (10, 20, 40),
    seeds: int = 10,
) -> list[BenchRow]:
    rows = []
    for method in methods:
        for per_hour in labels:
            results = [run_case(method, per_hour, seed) for seed in range(seeds)]
            rows.append(
                BenchRow(
                    method=method,
                    labels_per_hour=per_hour,
                    detection_hours=[d for d, _ in results],
                    false_rollbacks=sum(f for _, f in results),
                    runs=seeds,
                )
            )
    return rows


def to_markdown(rows: list[BenchRow]) -> str:
    lines = [
        "| Método | Rótulos/hora | v2 detectada | Horas até o rollback (mediana) | "
        "Rollbacks indevidos da v3 |",
        "|---|---|---|---|---|",
    ]
    for row in rows:
        median = row.median_hours()
        lines.append(
            f"| {row.method.value} | {row.labels_per_hour} | {len(row.detected)}/{row.runs} | "
            f"{'—' if median is None else f'{median:.0f} h'} | {row.false_rollbacks}/{row.runs} |"
        )
    return "\n".join(lines)

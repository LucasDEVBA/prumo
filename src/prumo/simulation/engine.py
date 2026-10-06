"""Simulador: avança o tempo hora a hora com leads sintéticos, revisores e o Prumo vigiando.

Serve para o demo local e para o benchmark: como o simulador conhece o gabarito, ele consegue
mostrar a "verdade" ao lado da estimativa, coisa que em produção ninguém vê.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta

from prumo.adapters.memory import InMemoryDecisionRepository, ManualClock
from prumo.container import Container
from prumo.data.leads import LeadGenerator, expected_class_for
from prumo.domain.errors import ConflictError, InvalidInputError
from prumo.domain.models import QualitySnapshot

logger = logging.getLogger(__name__)

KEEP_FOR_HUMANS = 3
"""Quantos itens sorteados por hora ficam na fila para uma pessoa de verdade rotular."""
STALE_AFTER = timedelta(hours=4)


@dataclass
class _TruthCounter:
    correct: int = 0
    total: int = 0

    @property
    def rate(self) -> float | None:
        return self.correct / self.total if self.total else None


@dataclass
class HourReport:
    snapshot: QualitySnapshot
    rolled_back_to: str | None


@dataclass
class Simulator:
    container: Container
    labels_per_hour: int = 20
    _truth: dict[str, _TruthCounter] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.container.is_simulated:
            raise ConflictError("o simulador só roda com PRUMO_PROVIDER=simulated")
        self._generator = LeadGenerator(self.container.rng)
        self._apply_rate()

    @property
    def leads_per_hour(self) -> int:
        return self.container.settings.simulation_leads_per_hour

    def set_labels_per_hour(self, value: int) -> None:
        if not 0 <= value <= self.leads_per_hour:
            raise InvalidInputError(
                f"rótulos por hora precisam estar entre 0 e {self.leads_per_hour}"
            )
        self.labels_per_hour = value
        self._apply_rate()

    def advance(self, hours: int = 1, *, quiet: bool = False) -> list[HourReport]:
        """Avança `hours` horas. `quiet` resume tudo numa linha (usado no aquecimento)."""
        reports = [self._one_hour(quiet=quiet) for _ in range(hours)]
        if quiet and reports:
            last = reports[-1].snapshot
            logger.info(
                "simulation_warmed_up",
                extra={
                    "hours": hours,
                    "prompt_version": last.prompt_version,
                    "status": last.status,
                    "point": last.point,
                },
            )
        return reports

    def _apply_rate(self) -> None:
        self.container.sampler.rate = self.labels_per_hour / self.leads_per_hour

    def _one_hour(self, *, quiet: bool) -> HourReport:
        c = self.container
        reviewer = c.reviewer
        assert reviewer is not None  # garantido por is_simulated  # noqa: S101
        version = c.prompts.active().id
        truth = self._truth.setdefault(version, _TruthCounter())
        sampled = []
        for _ in range(self.leads_per_hour):
            decision = c.decisions.process(self._generator.text())
            truth.total += 1
            truth.correct += decision.predicted == expected_class_for(decision.lead_text)
            if decision.sampled_for_review:
                sampled.append(decision)

        # A equipe simulada rotula quase tudo; alguns itens ficam para você no painel.
        for decision in sampled[KEEP_FOR_HUMANS:]:
            c.reviews.submit(
                decision.id,
                correct=reviewer.review(decision.lead_text, decision.predicted),
                reviewer="equipe-simulada",
                audit=False,
            )
        if isinstance(c.repository, InMemoryDecisionRepository):
            c.repository.discard_pending_older_than(c.clock.now() - STALE_AFTER)

        evaluation = c.quality.evaluate(truth=truth.rate)
        snapshot = evaluation.snapshot
        logger.log(
            logging.DEBUG if quiet else logging.INFO,
            "simulated_hour",
            extra={
                "simulated_at": snapshot.at.isoformat(),
                "prompt_version": snapshot.prompt_version,
                "status": snapshot.status,
                "decisions": self.leads_per_hour,
                "labels_by_team": max(0, len(sampled) - KEEP_FOR_HUMANS),
                "rolled_back_to": evaluation.rolled_back_to.id
                if evaluation.rolled_back_to
                else None,
            },
        )
        if isinstance(c.clock, ManualClock):
            c.clock.advance(timedelta(hours=1))
        return HourReport(
            snapshot=evaluation.snapshot,
            rolled_back_to=evaluation.rolled_back_to.id if evaluation.rolled_back_to else None,
        )

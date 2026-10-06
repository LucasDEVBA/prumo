"""A regra que decide reverter o prompt, compartilhada pelo alarme e pelo estimador horário.

Na AWS há dois gatilhos: o alarme do CloudWatch (caminho rápido, visível no console) e a
própria leitura horária do estimador (rede de segurança). O segundo existe porque a regra do
EventBridge só dispara na TRANSIÇÃO para ALARM: se uma tentativa fosse pulada, o alarme ficaria
parado em ALARM e nenhum evento novo chegaria. Os dois chamam a mesma regra, e ela é segura
contra repetição:

- só reverte com N leituras seguidas abaixo da meta, todas da versão no ar e posteriores ao
  último deploy/rollback (uma reentrega depois do rollback encontra leituras antigas e pula);
- a troca é compare-and-swap sobre a versão medida (dois gatilhos simultâneos: só um reverte).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from prumo.domain.errors import ConflictError
from prumo.domain.models import EventKind, QualitySnapshot
from prumo.ports import EventLog, PromptStore, SnapshotStore
from prumo.services.quality import QualityService
from prumo.stats.slo import SloStatus

logger = logging.getLogger(__name__)

EVENTS_LOOKBACK = 50
"""Quantos eventos recentes ler para achar o último deploy/rollback."""
VERSION_CHANGES = frozenset({EventKind.DEPLOY, EventKind.ROLLBACK})


@dataclass(frozen=True, slots=True)
class RollbackOutcome:
    action: str
    """rolled_back | skipped | disabled"""
    reason: str | None = None
    from_version: str | None = None
    to_version: str | None = None

    def as_dict(self) -> dict[str, str]:
        result = {"action": self.action}
        if self.reason:
            result["reason"] = self.reason
        if self.from_version:
            result["from"] = self.from_version
        if self.to_version:
            result["to"] = self.to_version
        return result


class RollbackGuard:
    NOT_CONSECUTIVE = "not_consecutive_for_active_version"
    VERSION_CONFLICT = "rollback_conflict"
    NO_PREVIOUS_VERSION = "no_previous_version"

    def __init__(
        self,
        *,
        quality: QualityService,
        prompts: PromptStore,
        snapshots: SnapshotStore,
        events: EventLog,
        enabled: bool,
    ) -> None:
        self._quality = quality
        self._prompts = prompts
        self._snapshots = snapshots
        self._events = events
        self.enabled = enabled

    def attempt(self, reason: str) -> RollbackOutcome:
        if not self.enabled:
            logger.info("rollback_disabled", extra={"trigger": reason})
            return RollbackOutcome("disabled")
        if self._prompts.previous() is None:
            logger.warning("rollback_skipped_no_previous_version", extra={"trigger": reason})
            return RollbackOutcome("skipped", self.NO_PREVIOUS_VERSION)
        measured = self.breached_version()
        if measured is None:
            return RollbackOutcome("skipped", self.NOT_CONSECUTIVE)
        try:
            restored = self._quality.rollback(reason=reason, expected_active=measured)
        except ConflictError as exc:
            logger.warning(
                "rollback_skipped_conflict", extra={"trigger": reason, "details": exc.details}
            )
            return RollbackOutcome("skipped", self.VERSION_CONFLICT)
        return RollbackOutcome("rolled_back", from_version=measured, to_version=restored.id)

    def breached_version(self) -> str | None:
        """A versão no ar, se ela tem N leituras seguidas abaixo da meta desde que entrou."""
        active = self._prompts.active().id
        required = self._quality.policy.slo.breaches_to_rollback
        readings = self._snapshots.recent(required)
        since = self._last_version_change()
        if len(readings) == required and all(
            _counts_as_breach(reading, active, since) for reading in readings
        ):
            return active
        logger.info(
            "rollback_skipped_not_consecutive",
            extra={
                "active": active,
                "required": required,
                "readings": [{"version": r.prompt_version, "status": r.status} for r in readings],
            },
        )
        return None

    def _last_version_change(self) -> datetime | None:
        for event in self._events.recent(EVENTS_LOOKBACK):
            if event.kind in VERSION_CHANGES:
                return event.at
        return None


def _counts_as_breach(reading: QualitySnapshot, active: str, since: datetime | None) -> bool:
    return (
        reading.status == SloStatus.BREACHED.value
        and reading.prompt_version == active
        and (since is None or reading.at > since)
    )

"""Lambda agendada de hora em hora: lê a qualidade, publica a métrica e confere o rollback.

1. Grava a leitura e imprime a métrica EMF `SloBreached` (é ela que o alarme observa).
2. Aplica o `RollbackGuard`, a mesma regra da Lambda de rollback. É a rede de segurança: o
   EventBridge só avisa na transição para ALARM, e se aquela tentativa tiver sido pulada, esta
   leitura tenta de novo. A regra é segura contra repetição, então os dois caminhos convivem.

O histórico em memória do `QualityService` não decide nada aqui: numa Lambda ele some a cada
ambiente novo. Quem decide é o guard, que lê as leituras persistidas no DynamoDB.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from functools import cache

from prumo.config import Settings
from prumo.container import Container, build_container
from prumo.lambdas._runtime import LambdaEvent, LambdaResult, bind_invocation
from prumo.observability import configure_logging
from prumo.services.quality import QualityService
from prumo.services.rollback_guard import RollbackGuard

logger = logging.getLogger(__name__)

ROLLBACK_REASON = "estimator"


@cache
def _container() -> Container:
    settings = Settings()
    configure_logging(settings.log_level)
    container = build_container(settings)
    container.quality.policy = replace(container.quality.policy, auto_rollback=False)
    return container


def run(quality: QualityService, guard: RollbackGuard | None = None) -> LambdaResult:
    snapshot = quality.evaluate().snapshot
    result: LambdaResult = {
        "prompt_version": snapshot.prompt_version,
        "status": snapshot.status,
        "point": snapshot.point,
        "lower": snapshot.lower,
        "upper": snapshot.upper,
        "n_labels": snapshot.n_labels,
        "n_decisions": snapshot.n_decisions,
    }
    if guard is not None:
        result["rollback"] = guard.attempt(ROLLBACK_REASON).as_dict()
    logger.info("quality_evaluated", extra=result)
    return result


def handler(event: LambdaEvent, context: object) -> LambdaResult:
    bind_invocation(context)
    container = _container()
    return run(container.quality, container.rollback_guard)

"""Lambda acionada pelo EventBridge quando o alarme de SLO muda para ALARM.

É o caminho rápido do rollback. A decisão em si (N leituras seguidas da versão no ar, troca com
compare-and-swap) fica no `RollbackGuard`, a mesma regra que o estimador aplica a cada hora como
rede de segurança. Por isso esta Lambda não precisa mexer no estado do alarme: se ela pular,
a próxima leitura horária tenta de novo.
"""

from __future__ import annotations

import logging
from functools import cache

from prumo.config import Settings
from prumo.container import Container, build_container
from prumo.lambdas._runtime import LambdaEvent, LambdaResult, bind_invocation
from prumo.observability import configure_logging

logger = logging.getLogger(__name__)

ALARM_DETAIL_TYPE = "CloudWatch Alarm State Change"
ALARM_STATE = "ALARM"
ROLLBACK_REASON = "cloudwatch_alarm"


@cache
def _container() -> Container:
    settings = Settings()
    configure_logging(settings.log_level)
    return build_container(settings)


def alarm_state(event: LambdaEvent) -> str | None:
    detail = event.get("detail")
    state = detail.get("state") if isinstance(detail, dict) else None
    value = state.get("value") if isinstance(state, dict) else None
    return value if isinstance(value, str) else None


def handle_alarm(event: LambdaEvent, container: Container) -> LambdaResult:
    state = alarm_state(event)
    if event.get("detail-type") != ALARM_DETAIL_TYPE or state != ALARM_STATE:
        logger.info("alarm_event_ignored", extra={"alarm_state": state})
        return {"action": "ignored", "alarm_state": state}
    outcome = container.rollback_guard.attempt(ROLLBACK_REASON)
    logger.info("alarm_handled", extra=outcome.as_dict())
    return dict(outcome.as_dict())


def handler(event: LambdaEvent, context: object) -> LambdaResult:
    bind_invocation(context)
    return handle_alarm(event, _container())

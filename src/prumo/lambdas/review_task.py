"""Lambda da tarefa `waitForTaskToken` do Step Functions: guarda o token na decisão.

Entrada (montada pela máquina de estados): `{"decision_id": "...", "task_token": "$$.Task.Token"}`.
O token é uma credencial da execução: nunca vai para log nem para mensagem de erro.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from functools import cache
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from prumo.adapters.aws.dynamodb import DynamoDecisionRepository
from prumo.adapters.aws.stepfunctions import send_review_result, sfn_client
from prumo.config import Settings
from prumo.domain.errors import InvalidInputError
from prumo.domain.models import Decision
from prumo.lambdas._runtime import LambdaEvent, LambdaResult, bind_invocation
from prumo.observability import configure_logging

if TYPE_CHECKING:
    from mypy_boto3_stepfunctions import SFNClient

logger = logging.getLogger(__name__)

MAX_TASK_TOKEN_CHARS = 2048
MAX_DECISION_ID_CHARS = 80


class ReviewTaskEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    decision_id: str = Field(min_length=1, max_length=MAX_DECISION_ID_CHARS)
    task_token: str = Field(min_length=1, max_length=MAX_TASK_TOKEN_CHARS)


class DecisionTokens(Protocol):
    def get(self, decision_id: str) -> Decision | None: ...

    def save_task_token(self, decision_id: str, task_token: str) -> None: ...


type CompleteReview = Callable[[str, Decision], None]


def parse_event(event: LambdaEvent) -> ReviewTaskEvent:
    try:
        return ReviewTaskEvent.model_validate(event)
    except ValidationError as exc:
        # Só os nomes dos campos: a mensagem do Pydantic repetiria o valor (o token).
        fields = sorted({".".join(str(part) for part in err["loc"]) for err in exc.errors()})
        raise InvalidInputError(
            "evento da tarefa de revisão inválido", details={"fields": fields}
        ) from exc


def handle_review_task(
    event: LambdaEvent, store: DecisionTokens, complete: CompleteReview
) -> LambdaResult:
    request = parse_event(event)
    store.save_task_token(request.decision_id, request.task_token)
    decision = store.get(request.decision_id)
    if decision is not None and decision.human_correct is not None:
        # Rotulada antes de o token existir: conclui já, senão a execução esperaria até o prazo.
        complete(request.task_token, decision)
        logger.info("review_task_completed_on_arrival", extra={"decision_id": decision.id})
        return {"decision_id": request.decision_id, "status": "completed"}
    logger.info("review_task_waiting", extra={"decision_id": request.decision_id})
    return {"decision_id": request.decision_id, "status": "waiting"}


@cache
def _dependencies() -> tuple[DynamoDecisionRepository, SFNClient]:
    settings = Settings()
    configure_logging(settings.log_level)
    return DynamoDecisionRepository.from_settings(settings), sfn_client(settings)


def handler(event: LambdaEvent, context: object) -> LambdaResult:
    bind_invocation(context)
    repository, client = _dependencies()
    return handle_review_task(
        event, repository, lambda token, decision: send_review_result(client, token, decision)
    )

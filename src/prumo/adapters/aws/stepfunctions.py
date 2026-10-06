"""Fluxo de revisão humana no AWS Step Functions (tarefa com `waitForTaskToken`).

Cada decisão sorteada abre uma execução com o nome = id da decisão. A primeira tarefa da
máquina guarda o task token na decisão (DynamoDB) e a execução espera. Quando o humano rotula,
`complete_review` devolve o resultado com `SendTaskSuccess` e a execução segue.

O rótulo em si já está gravado no repositório antes disso: o Step Functions orquestra lembretes
e prazos, mas a estimativa de qualidade nunca depende dele. Por isso as falhas daqui saem como
`ProviderError` e quem chama (o serviço) as registra como `review_workflow_failed` sem desfazer
nada. Rótulo "errado" também é `SendTaskSuccess`: o fluxo nunca usa `SendTaskFailure`.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Protocol

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from prumo.adapters.aws._common import client_config, error_code, provider_error
from prumo.adapters.aws.dynamodb import DynamoDecisionRepository
from prumo.config import Storage
from prumo.domain.models import Decision

if TYPE_CHECKING:
    from mypy_boto3_stepfunctions import SFNClient

    from prumo.config import Settings

logger = logging.getLogger(__name__)

SERVICE = "stepfunctions"

_STALE_TOKEN_CODES = frozenset({"TaskTimedOut", "InvalidToken", "TaskDoesNotExist"})
"""A execução já expirou, já foi concluída ou o token não vale mais. Não há o que fazer: o
rótulo já está salvo, então isso vira aviso no log, não erro para quem revisou."""


class TaskTokenStore(Protocol):
    """Onde o task token de cada decisão fica guardado (na AWS, o DynamoDB)."""

    def save_task_token(self, decision_id: str, task_token: str) -> None: ...

    def task_token_for(self, decision_id: str) -> str | None: ...


def sfn_client(settings: Settings) -> SFNClient:
    return boto3.client("stepfunctions", region_name=settings.aws_region, config=client_config())


def review_output(decision: Decision) -> str:
    """Saída entregue à execução: o suficiente para os próximos estados, sem o texto do lead."""
    return json.dumps(
        {
            "decision_id": decision.id,
            "human_correct": decision.human_correct,
            "reviewer": decision.reviewer,
        }
    )


def send_review_result(client: SFNClient, task_token: str, decision: Decision) -> None:
    """Conclui a tarefa que espera a revisão. Token vencido/inválido só gera aviso."""
    try:
        client.send_task_success(taskToken=task_token, output=review_output(decision))
    except ClientError as exc:
        code = error_code(exc)
        if code not in _STALE_TOKEN_CODES:
            raise provider_error(exc, service=SERVICE, operation="SendTaskSuccess") from exc
        logger.warning(
            "review_task_token_stale", extra={"decision_id": decision.id, "error_code": code}
        )
        return
    except BotoCoreError as exc:
        raise provider_error(exc, service=SERVICE, operation="SendTaskSuccess") from exc
    logger.info("review_task_completed", extra={"decision_id": decision.id})


class StepFunctionsReviewWorkflow:
    """Porta `ReviewWorkflow` sobre uma máquina de estados do Step Functions."""

    def __init__(
        self, *, client: SFNClient, state_machine_arn: str, token_store: TaskTokenStore
    ) -> None:
        self._client = client
        self._state_machine_arn = state_machine_arn
        self._tokens = token_store

    @classmethod
    def from_settings(cls, settings: Settings) -> StepFunctionsReviewWorkflow:
        if not settings.review_state_machine_arn:
            raise ValueError("PRUMO_REVIEW_STATE_MACHINE_ARN não configurado")
        if settings.storage is not Storage.DYNAMODB:
            # O token fica na decisão; com decisões em memória, a tarefa nunca acharia a decisão.
            raise ValueError("o fluxo de revisão no Step Functions exige PRUMO_STORAGE=dynamodb")
        return cls(
            client=sfn_client(settings),
            state_machine_arn=settings.review_state_machine_arn,
            token_store=DynamoDecisionRepository.from_settings(settings),
        )

    def request_review(self, decision: Decision) -> None:
        """Abre a execução. O nome é o id da decisão, então repetir a chamada não duplica."""
        try:
            response = self._client.start_execution(
                stateMachineArn=self._state_machine_arn,
                name=decision.id,
                input=json.dumps({"decision_id": decision.id}),
            )
        except ClientError as exc:
            if error_code(exc) != "ExecutionAlreadyExists":
                raise provider_error(exc, service=SERVICE, operation="StartExecution") from exc
            logger.info("review_execution_already_exists", extra={"decision_id": decision.id})
            return
        except BotoCoreError as exc:
            raise provider_error(exc, service=SERVICE, operation="StartExecution") from exc
        logger.info(
            "review_execution_started",
            extra={"decision_id": decision.id, "execution_arn": response["executionArn"]},
        )

    def complete_review(self, decision: Decision) -> None:
        token = self._tokens.task_token_for(decision.id)
        if token is None:
            # A tarefa ainda não gravou o token: ela mesma conclui ao ver o rótulo já salvo.
            logger.warning("review_task_token_missing", extra={"decision_id": decision.id})
            return
        send_review_result(self._client, token, decision)

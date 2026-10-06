"""Step Functions da revisão humana: espera o rótulo por até 72 h com waitForTaskToken."""

from __future__ import annotations

from aws_cdk import Duration, RemovalPolicy
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk import aws_stepfunctions_tasks as tasks
from constructs import Construct

from prumo_infra.config import LOG_RETENTION

REVIEW_TIMEOUT = Duration.hours(72)
# Folga sobre o timeout da tarefa: só pega execução presa por algo fora do fluxo esperado.
EXECUTION_TIMEOUT = Duration.hours(73)
# Falha da Lambda que guarda o token (ex.: DynamoDB indisponível por alguns segundos). Cada
# tentativa gera um token novo, e a Lambda grava o mais recente; a decisão não fica órfã.
TASK_FAILED_RETRY_ATTEMPTS = 3
TASK_FAILED_RETRY_INTERVAL = Duration.seconds(2)
TASK_FAILED_BACKOFF_RATE = 2


class ReviewWorkflow(Construct):
    """AguardarRevisao (Lambda + task token) → RevisaoConcluida, ou RevisaoExpirada no timeout."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        review_task: lambda_.IFunction,
        removal_policy: RemovalPolicy,
    ) -> None:
        super().__init__(scope, construct_id)
        log_group = logs.LogGroup(
            self, "Logs", retention=LOG_RETENTION, removal_policy=removal_policy
        )
        self.state_machine = sfn.StateMachine(
            self,
            "StateMachine",
            state_machine_type=sfn.StateMachineType.STANDARD,
            definition_body=sfn.DefinitionBody.from_chainable(self._definition(review_task)),
            timeout=EXECUTION_TIMEOUT,
            tracing_enabled=True,
            comment="Revisão humana de uma decisão sorteada",
            # Sem dados de execução no log: o payload tem texto de lead e o task token.
            logs=sfn.LogOptions(
                destination=log_group, level=sfn.LogLevel.ERROR, include_execution_data=False
            ),
            removal_policy=removal_policy,
        )

    def _definition(self, review_task: lambda_.IFunction) -> sfn.IChainable:
        wait_for_review = tasks.LambdaInvoke(
            self,
            "AguardarRevisao",
            lambda_function=review_task,
            integration_pattern=sfn.IntegrationPattern.WAIT_FOR_TASK_TOKEN,
            payload=sfn.TaskInput.from_object(
                {
                    "decision_id": sfn.JsonPath.string_at("$.decision_id"),
                    "task_token": sfn.JsonPath.task_token,
                }
            ),
            task_timeout=sfn.Timeout.duration(REVIEW_TIMEOUT),
            result_path="$.revisao",
        )
        expired = sfn.Succeed(
            self,
            "RevisaoExpirada",
            comment="Ninguém rotulou em 72 h: a decisão segue só com o veredito do juiz",
        )
        # States.TaskFailed não casa com States.Timeout: as 72 h sem rótulo continuam indo para
        # o catch, sem repetir a espera.
        wait_for_review.add_retry(
            errors=["States.TaskFailed"],
            max_attempts=TASK_FAILED_RETRY_ATTEMPTS,
            interval=TASK_FAILED_RETRY_INTERVAL,
            backoff_rate=TASK_FAILED_BACKOFF_RATE,
        )
        wait_for_review.add_catch(expired, errors=["States.Timeout"], result_path="$.erro")
        return wait_for_review.next(sfn.Succeed(self, "RevisaoConcluida"))

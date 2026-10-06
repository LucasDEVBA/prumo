"""Agendamento do estimador, alarme de SLO → rollback, e alarmes de quem vigia o vigia."""

from __future__ import annotations

from dataclasses import dataclass

from aws_cdk import Aws, Duration, RemovalPolicy
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_scheduler as scheduler
from aws_cdk import aws_scheduler_targets as scheduler_targets
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subscriptions
from aws_cdk import aws_sqs as sqs
from aws_cdk import aws_stepfunctions as sfn
from constructs import Construct

METRICS_NAMESPACE = "Prumo"
SLO_METRIC = "SloBreached"
READING_PERIOD = Duration.hours(1)
BREACHED_READINGS_TO_ROLLBACK = 2
DEAD_MAN_PERIOD = Duration.hours(2)
FAILURE_PERIOD = Duration.minutes(5)
DEAD_LETTER_RETENTION = Duration.days(14)
ROLLBACK_EVENT_RETRIES = 2
# Variável que a Lambda de rollback lê para re-armar o alarme quando decide não reverter.
# Evento de log JSON que o serviço emite quando a revisão humana não pôde ser agendada/concluída.
REVIEW_WORKFLOW_FAILED_EVENT = "review_workflow_failed"
REVIEW_WORKFLOW_FAILED_METRIC = "ReviewWorkflowFailed"


@dataclass(frozen=True, slots=True)
class MonitoredResources:
    """O que o Monitoring observa ou aciona."""

    estimator: lambda_.IFunction
    rollback: lambda_.Function
    rollback_dead_letters: sqs.IQueue
    api_log_group: logs.ILogGroup
    review_state_machine: sfn.IStateMachine
    review_task: lambda_.IFunction


def rollback_dead_letter_queue(scope: Construct, removal_policy: RemovalPolicy) -> sqs.Queue:
    """Fila do evento de rollback que não chegou (EventBridge) ou falhou em todas as tentativas.

    Sem ela, um rollback perdido não deixa rastro: o alarme fica em ALARM e não gera outro evento.
    """
    return sqs.Queue(
        scope,
        "RollbackDeadLetters",
        encryption=sqs.QueueEncryption.SQS_MANAGED,
        enforce_ssl=True,
        retention_period=DEAD_LETTER_RETENTION,
        removal_policy=removal_policy,
    )


class Monitoring(Construct):
    """Tudo que observa o Prumo e reage sozinho. Alertas humanos saem pelo tópico SNS."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        resources: MonitoredResources,
        alert_email: str | None,
    ) -> None:
        super().__init__(scope, construct_id)
        self.topic = self._alerts_topic(alert_email)
        self._alarm_action = cw_actions.SnsAction(self.topic)
        self._schedule_estimator(resources.estimator)
        self.slo_alarm = self._slo_alarm()
        self._rollback_on_alarm(resources)
        self.dead_man_alarm = self._dead_man_alarm(resources.estimator)
        self._errors_alarm("EstimatorErrors", resources.estimator, READING_PERIOD, "estimador")
        self._errors_alarm("RollbackErrors", resources.rollback, FAILURE_PERIOD, "rollback")
        self._dead_letters_alarm(resources.rollback_dead_letters)
        self._review_alarms(resources)

    def _alerts_topic(self, alert_email: str | None) -> sns.Topic:
        topic = sns.Topic(self, "Alerts", display_name="Prumo - alertas", enforce_ssl=True)
        # O enforce_ssl cria uma TopicPolicy só com o Deny de transporte sem TLS, e ela substitui
        # a policy padrão do tópico. Sem este Allow o CloudWatch não consegue publicar e nenhum
        # alarme chega a ninguém (o alarme muda de estado e a ação falha em silêncio).
        topic.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AllowCloudWatchAlarmsToPublish",
                principals=[iam.ServicePrincipal("cloudwatch.amazonaws.com")],
                actions=["sns:Publish"],
                resources=[topic.topic_arn],
                conditions={
                    "ArnLike": {
                        "aws:SourceArn": (
                            f"arn:{Aws.PARTITION}:cloudwatch:{Aws.REGION}:{Aws.ACCOUNT_ID}:alarm:*"
                        )
                    },
                    "StringEquals": {"aws:SourceAccount": Aws.ACCOUNT_ID},
                },
            )
        )
        if alert_email:
            topic.add_subscription(subscriptions.EmailSubscription(alert_email))
        return topic

    def _schedule_estimator(self, estimator: lambda_.IFunction) -> None:
        scheduler.Schedule(
            self,
            "EstimatorHourly",
            description="Leitura horária da qualidade do prompt ativo",
            schedule=scheduler.ScheduleExpression.rate(READING_PERIOD),
            target=scheduler_targets.LambdaInvoke(
                estimator, retry_attempts=2, max_event_age=Duration.minutes(30)
            ),
        )

    def _slo_alarm(self) -> cloudwatch.Alarm:
        # Sem dimensões: a linha EMF também publica o agregado ([]), que não muda de nome quando
        # a versão do prompt muda. 2 de 2 leituras = a regra de rollback do Prumo; com o
        # histórico no alarme, ela sobrevive ao estimador rodar cada hora numa Lambda nova.
        alarm = cloudwatch.Alarm(
            self,
            "SloBreachedAlarm",
            alarm_description="Faixa de confiança da taxa de acerto abaixo da meta em 2 leituras",
            metric=cloudwatch.Metric(
                namespace=METRICS_NAMESPACE,
                metric_name=SLO_METRIC,
                statistic=cloudwatch.Stats.MAXIMUM,
                period=READING_PERIOD,
            ),
            threshold=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            evaluation_periods=BREACHED_READINGS_TO_ROLLBACK,
            datapoints_to_alarm=BREACHED_READINGS_TO_ROLLBACK,
            # Sem leitura não há evidência de piora; quem cobre "sem leitura" é o dead-man.
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        alarm.add_alarm_action(self._alarm_action)
        return alarm

    def _rollback_on_alarm(self, resources: MonitoredResources) -> None:
        rollback = resources.rollback
        events.Rule(
            self,
            "RollbackOnSloBreach",
            description="Alarme de SLO em ALARM → Lambda de rollback do prompt",
            event_pattern=events.EventPattern(
                source=["aws.cloudwatch"],
                detail_type=["CloudWatch Alarm State Change"],
                resources=[self.slo_alarm.alarm_arn],
                detail={"state": {"value": ["ALARM"]}},
            ),
            targets=[
                targets.LambdaFunction(
                    rollback,
                    retry_attempts=ROLLBACK_EVENT_RETRIES,
                    dead_letter_queue=resources.rollback_dead_letters,
                )
            ],
        )
        # O alarme em ALARM não gera outro evento. Por isso o estimador aplica a mesma regra a
        # cada hora: se esta tentativa for pulada, a próxima leitura tenta de novo, sem precisar
        # mexer no estado do alarme (o que criaria um ciclo de ALARM/OK e e-mails).

    def _dead_man_alarm(self, estimator: lambda_.IFunction) -> cloudwatch.Alarm:
        # Ausência de erro não prova que a leitura está rodando: sem invocação, sem métrica, e o
        # alarme de SLO ficaria verde para sempre (NOT_BREACHING).
        alarm = cloudwatch.Alarm(
            self,
            "EstimatorSilentAlarm",
            alarm_description="Estimador sem nenhuma invocação em 2 horas (dead-man's switch)",
            metric=estimator.metric_invocations(
                period=DEAD_MAN_PERIOD, statistic=cloudwatch.Stats.SUM
            ),
            threshold=1,
            comparison_operator=cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
            evaluation_periods=1,
            treat_missing_data=cloudwatch.TreatMissingData.BREACHING,
        )
        alarm.add_alarm_action(self._alarm_action)
        return alarm

    def _dead_letters_alarm(self, queue: sqs.IQueue) -> cloudwatch.Alarm:
        return self._at_least_one(
            "RollbackDeadLettersAlarm",
            "Prumo: evento de rollback na DLQ (o prompt ruim pode ter ficado no ar)",
            queue.metric_approximate_number_of_messages_visible(
                period=FAILURE_PERIOD, statistic=cloudwatch.Stats.MAXIMUM
            ),
        )

    def _review_alarms(self, resources: MonitoredResources) -> None:
        workflow_failed = logs.MetricFilter(
            self,
            "ReviewWorkflowFailedFilter",
            log_group=resources.api_log_group,
            filter_pattern=logs.FilterPattern.string_value(
                "$.action", "=", REVIEW_WORKFLOW_FAILED_EVENT
            ),
            metric_namespace=METRICS_NAMESPACE,
            metric_name=REVIEW_WORKFLOW_FAILED_METRIC,
            metric_value="1",
        )
        # O serviço engole a falha para não derrubar a classificação; sem este alarme, decisões
        # sorteadas deixariam de ir para revisão humana e a estimativa perderia rótulos calada.
        self._at_least_one(
            "ReviewWorkflowFailedAlarm",
            "Prumo: revisão humana não foi agendada/concluída (review_workflow_failed)",
            workflow_failed.metric(period=FAILURE_PERIOD, statistic=cloudwatch.Stats.SUM),
        )
        self._at_least_one(
            "ReviewExecutionsFailedAlarm",
            "Prumo: execução da revisão humana falhou no Step Functions",
            resources.review_state_machine.metric_failed(
                period=FAILURE_PERIOD, statistic=cloudwatch.Stats.SUM
            ),
        )
        self._errors_alarm("ReviewTaskErrors", resources.review_task, FAILURE_PERIOD, "revisão")

    def _errors_alarm(
        self, construct_id: str, function: lambda_.IFunction, period: Duration, what: str
    ) -> cloudwatch.Alarm:
        return self._at_least_one(
            construct_id,
            f"Prumo: Lambda de {what} falhou",
            function.metric_errors(period=period, statistic=cloudwatch.Stats.SUM),
        )

    def _at_least_one(
        self, construct_id: str, description: str, metric: cloudwatch.IMetric
    ) -> cloudwatch.Alarm:
        alarm = cloudwatch.Alarm(
            self,
            construct_id,
            alarm_description=description,
            metric=metric,
            threshold=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            evaluation_periods=1,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        alarm.add_alarm_action(self._alarm_action)
        return alarm

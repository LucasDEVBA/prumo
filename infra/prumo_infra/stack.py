"""PrumoStack: monta tabela, AppConfig, Lambdas, API, revisão humana, alarmes e orçamento."""

from __future__ import annotations

from collections.abc import Mapping

from aws_cdk import Aws, CfnOutput, Environment, Stack
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_lambda_destinations as destinations
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct

from prumo_infra.api import PublicApi
from prumo_infra.bedrock import ArnContext, ModelGrant, guardrail_arn, model_grants
from prumo_infra.budget import CostBudget
from prumo_infra.config import StackConfig
from prumo_infra.functions import (
    API,
    ESTIMATOR,
    REVIEW_TASK,
    ROLLBACK,
    FunctionSpec,
    build_function,
    lambda_code,
)
from prumo_infra.monitoring import (
    METRICS_NAMESPACE,
    MonitoredResources,
    Monitoring,
    rollback_dead_letter_queue,
)
from prumo_infra.prompts import PromptConfig
from prumo_infra.review import ReviewWorkflow

# Ações mínimas na tabela por papel, conforme src/prumo/adapters/aws/dynamodb.py. Sem Scan:
# toda leitura é por chave. TransactWriteItems não tem ação IAM própria; é autorizado pelas
# ações de cada item da transação (aqui, PutItem e UpdateItem).
TABLE_ACTIONS_API = (
    "dynamodb:GetItem",
    "dynamodb:Query",
    "dynamodb:PutItem",
    "dynamodb:UpdateItem",
)
TABLE_ACTIONS_JOBS = ("dynamodb:GetItem", "dynamodb:Query", "dynamodb:PutItem")
TABLE_ACTIONS_REVIEW_TASK = ("dynamodb:GetItem", "dynamodb:UpdateItem")
# Índice esparso que é a fila de revisão (só decisões pendentes têm gsi1pk).
REVIEW_QUEUE_INDEX = "gsi1"
# SendTaskSuccess não aceita ARN no IAM (a AWS não oferece permissão por recurso): o escopo real
# é o próprio task token, uma credencial imprevisível de UMA execução. SendTaskFailure fica de
# fora porque o código não usa (rótulo "errado" também é SendTaskSuccess).
TASK_TOKEN_ACTIONS = ("states:SendTaskSuccess",)
# Converse e InvokeModel são autorizados pela mesma ação IAM (não existe "bedrock:Converse").
BEDROCK_INVOKE_ACTIONS = ("bedrock:InvokeModel",)
ADMIN_TOKEN_LENGTH = 48
# Classificador e juiz rodam em sequência na mesma requisição, e o API Gateway corta em 30 s
# (contando o cold start). Pior caso: 2 chamadas x tentativas x (connect 3 s + read). Com 8 s de
# leitura só cabe UMA tentativa: 2 x 1 x 11 = 22 s, sobram 7 s para init e DynamoDB/AppConfig.
# Duas tentativas exigiriam leitura de 3 s, abaixo da latência normal do juiz (Haiku).
# O teste test_should_fit_bedrock_worst_case_inside_api_lambda_timeout trava a conta.
API_BEDROCK_MAX_ATTEMPTS = 1
API_BEDROCK_TIMEOUT_SECONDS = 8
# Texto do lead é dado pessoal (LGPD): a decisão expira e o DynamoDB apaga sozinho.
DECISION_TTL_DAYS = 180
DECISION_TTL_ATTRIBUTE = "expires_at"


class PrumoStack(Stack):
    """A stack inteira do Prumo. Os parâmetros variáveis chegam prontos em `config`."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: StackConfig,
        env: Environment | None = None,
    ) -> None:
        super().__init__(
            scope,
            construct_id,
            env=env,
            description="Prumo: taxa real de acerto da IA em produção e rollback do prompt",
            tags={"project": "prumo"},
        )
        self._config = config
        self.table = self._decisions_table()
        self.prompts = PromptConfig(
            self, "Prompts", dev=config.dev, removal_policy=config.removal_policy
        )
        self.admin_token = self._admin_token()
        self._code = lambda_code(config.lambda_asset_path)
        # A máquina de estados precisa da review_task, e a API precisa do ARN da máquina.
        self.review_task = self._function(REVIEW_TASK)
        self.review = ReviewWorkflow(
            self, "Review", review_task=self.review_task, removal_policy=config.removal_policy
        )
        self.api_function = self._function(API, self._api_only_environment())
        self.estimator = self._function(ESTIMATOR)
        self.rollback_dead_letters = rollback_dead_letter_queue(self, config.removal_policy)
        self.rollback = self._function(
            ROLLBACK, on_failure=destinations.SqsDestination(self.rollback_dead_letters)
        )
        self._grant_permissions()
        self.api = self._public_api()
        self.monitoring = Monitoring(
            self, "Monitoring", resources=self._monitored(), alert_email=config.alert_email
        )
        if config.budget_email:
            CostBudget(self, "Budget", email=config.budget_email, limit_usd=config.budget_usd)
        self._outputs()

    def _decisions_table(self) -> dynamodb.Table:
        table = dynamodb.Table(
            self,
            "DecisionsTable",
            partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="sk", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            deletion_protection=not self._config.dev,
            removal_policy=self._config.removal_policy,
            time_to_live_attribute=DECISION_TTL_ATTRIBUTE,
        )
        table.add_global_secondary_index(
            index_name=REVIEW_QUEUE_INDEX,
            partition_key=dynamodb.Attribute(name="gsi1pk", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="gsi1sk", type=dynamodb.AttributeType.STRING),
            projection_type=dynamodb.ProjectionType.ALL,
        )
        return table

    def _monitored(self) -> MonitoredResources:
        return MonitoredResources(
            estimator=self.estimator,
            rollback=self.rollback,
            rollback_dead_letters=self.rollback_dead_letters,
            api_log_group=self.api_function.log_group,
            review_state_machine=self.review.state_machine,
            review_task=self.review_task,
        )

    def _public_api(self) -> PublicApi:
        config = self._config
        return PublicApi(
            self,
            "Api",
            handler=self.api_function,
            allowed_origins=config.allowed_origins,
            rate_limit=config.api_rate_limit,
            burst_limit=config.api_burst_limit,
            removal_policy=config.removal_policy,
        )

    def _admin_token(self) -> secretsmanager.Secret:
        # Gerado pela AWS: o token nunca passa pelo código, pelo template nem pelo CI.
        return secretsmanager.Secret(
            self,
            "AdminToken",
            description="Token exigido no header X-Prumo-Token nas rotas que mudam estado",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                password_length=ADMIN_TOKEN_LENGTH, exclude_punctuation=True
            ),
            removal_policy=self._config.removal_policy,
        )

    def _shared_environment(self) -> dict[str, str]:
        config = self._config
        environment = {
            "PRUMO_PROVIDER": "bedrock",
            "PRUMO_STORAGE": "dynamodb",
            "PRUMO_PROMPT_BACKEND": "appconfig",
            "PRUMO_DECISIONS_TABLE": self.table.table_name,
            "PRUMO_APPCONFIG_APPLICATION": self.prompts.application_id,
            "PRUMO_APPCONFIG_ENVIRONMENT": self.prompts.environment_id,
            "PRUMO_APPCONFIG_PROFILE": self.prompts.profile_id,
            "PRUMO_APPCONFIG_DEPLOYMENT_STRATEGY": self.prompts.strategy_id,
            "PRUMO_METRICS_NAMESPACE": METRICS_NAMESPACE,
            "PRUMO_AWS_REGION": Aws.REGION,
            "PRUMO_BEDROCK_CLASSIFIER_MODEL": config.classifier_model,
            "PRUMO_BEDROCK_JUDGE_MODEL": config.judge_model,
            # Chave geral do rollback automático (alarme e estimador usam a mesma regra, segura
            # contra repetição). "false" desliga os dois caminhos sem mexer no EventBridge.
            "PRUMO_AUTO_ROLLBACK": "true",
            "PRUMO_DECISION_TTL_DAYS": str(DECISION_TTL_DAYS),
            "PRUMO_LOG_LEVEL": "INFO",
        }
        if config.guardrail_id:
            environment["PRUMO_BEDROCK_GUARDRAIL_ID"] = config.guardrail_id
            environment["PRUMO_BEDROCK_GUARDRAIL_VERSION"] = config.guardrail_version
        return environment

    def _api_only_environment(self) -> dict[str, str]:
        return {
            "PRUMO_REVIEW_STATE_MACHINE_ARN": self.review.state_machine.state_machine_arn,
            # Só o ARN: a Lambda lê o valor no cold start. Resolver o segredo numa env var o
            # deixaria legível por quem tem lambda:GetFunctionConfiguration (console, CLI, logs
            # de auditoria) e congelado até o próximo deploy, impedindo a rotação.
            "PRUMO_ADMIN_TOKEN_SECRET_ARN": self.admin_token.secret_arn,
            "PRUMO_BEDROCK_MAX_ATTEMPTS": str(API_BEDROCK_MAX_ATTEMPTS),
            "PRUMO_BEDROCK_TIMEOUT_SECONDS": str(API_BEDROCK_TIMEOUT_SECONDS),
        }

    def _function(
        self,
        spec: FunctionSpec,
        extra_environment: Mapping[str, str] | None = None,
        *,
        on_failure: lambda_.IDestination | None = None,
    ) -> lambda_.Function:
        return build_function(
            self,
            spec,
            code=self._code,
            environment={**self._shared_environment(), **(extra_environment or {})},
            removal_policy=self._config.removal_policy,
            on_failure=on_failure,
        )

    def _grant_permissions(self) -> None:
        self.table.grant(self.api_function, *TABLE_ACTIONS_API)
        for job in (self.estimator, self.rollback):
            self.table.grant(job, *TABLE_ACTIONS_JOBS)
        self.table.grant(self.review_task, *TABLE_ACTIONS_REVIEW_TASK)

        for reader in (self.api_function, self.estimator, self.rollback):
            self.prompts.grant_read(reader)
        # O estimador também publica: ele é a rede de segurança do rollback a cada hora.
        for deployer in (self.api_function, self.rollback, self.estimator):
            self.prompts.grant_deploy(deployer)

        self.review.state_machine.grant_start_execution(self.api_function)
        for completer in (self.api_function, self.review_task):
            _grant_any_resource(completer, TASK_TOKEN_ACTIONS)
        self.admin_token.grant_read(self.api_function)
        self._grant_bedrock(self.api_function)

    def _grant_bedrock(self, grantee: iam.IGrantable) -> None:
        ctx = ArnContext(partition=Aws.PARTITION, region=Aws.REGION, account=Aws.ACCOUNT_ID)
        grants: list[ModelGrant] = []
        for model in (self._config.classifier_model, self._config.judge_model):
            grants.extend(g for g in model_grants(model, ctx) if g not in grants)
        for grant in grants:
            # Um statement por grant: a condição de um profile não pode valer para outro modelo.
            iam.Grant.add_to_principal(
                grantee=grantee,
                actions=list(BEDROCK_INVOKE_ACTIONS),
                resource_arns=list(grant.resource_arns),
                conditions=grant.conditions,
            )
        if self._config.guardrail_id:
            iam.Grant.add_to_principal(
                grantee=grantee,
                actions=["bedrock:ApplyGuardrail"],
                resource_arns=[guardrail_arn(self._config.guardrail_id, ctx)],
            )

    def _outputs(self) -> None:
        CfnOutput(self, "ApiUrl", value=self.api.url, description="URL base da API")
        CfnOutput(self, "DecisionsTableName", value=self.table.table_name)
        CfnOutput(self, "ReviewStateMachineArn", value=self.review.state_machine.state_machine_arn)
        CfnOutput(self, "AlertsTopicArn", value=self.monitoring.topic.topic_arn)
        CfnOutput(
            self,
            "AdminTokenSecretArn",
            value=self.admin_token.secret_arn,
            description="aws secretsmanager get-secret-value --secret-id <este ARN>",
        )


def _grant_any_resource(grantee: iam.IGrantable, actions: tuple[str, ...]) -> None:
    """Só para ações que o IAM não deixa restringir por ARN (ver TASK_TOKEN_ACTIONS_*)."""
    iam.Grant.add_to_principal(grantee=grantee, actions=list(actions), resource_arns=["*"])

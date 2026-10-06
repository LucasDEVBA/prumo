"""AppConfig: guarda qual versão do prompt está ativa e o histórico usado pelo rollback."""

from __future__ import annotations

import json

from aws_cdk import Aws, RemovalPolicy
from aws_cdk import aws_appconfig as appconfig
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from aws_cdk import custom_resources as cr
from constructs import Construct

from prumo_infra.config import LOG_RETENTION

# Espelha PROMPT_VERSION_PATTERN de src/prumo/domain/models.py (o teste da infra confere).
PROMPT_VERSION_PATTERN = r"^v[0-9]{1,4}$"
INITIAL_VERSION = "v1"
INITIAL_DOCUMENT = {"active": INITIAL_VERSION, "history": [INITIAL_VERSION]}
MAX_HISTORY = 50  # o mesmo teto do adaptador

# additionalProperties fica livre: se o adaptador passar a gravar um campo novo, a validação não
# pode barrar justamente o deploy de um rollback.
PROMPT_DOCUMENT_SCHEMA = {
    "$schema": "http://json-schema.org/draft-04/schema#",
    "type": "object",
    "required": ["active", "history"],
    "properties": {
        "active": {"type": "string", "pattern": PROMPT_VERSION_PATTERN},
        "history": {
            "type": "array",
            "minItems": 1,
            "maxItems": MAX_HISTORY,
            "items": {"type": "string", "pattern": PROMPT_VERSION_PATTERN},
        },
    },
}

READ_ACTIONS = ("appconfig:StartConfigurationSession", "appconfig:GetLatestConfiguration")
DEPLOY_ACTIONS = ("appconfig:CreateHostedConfigurationVersion", "appconfig:StartDeployment")
# Trava otimista do adaptador: antes de publicar, ele confere no plano de controle (consistente,
# ao contrário do plano de dados, que tem cache) qual versão está implantada e o conteúdo dela.
OPTIMISTIC_LOCK_ACTIONS = (
    "appconfig:ListDeployments",
    "appconfig:GetHostedConfigurationVersion",
)
# O adaptador (src/prumo/adapters/aws/appconfig.py) resolve nome→id listando antes de publicar.
RESOLVE_ACTIONS = ("appconfig:ListEnvironments", "appconfig:ListConfigurationProfiles")
# ListApplications não aceita ARN no IAM; devolve só nomes e ids de aplicações, nenhum conteúdo.
LIST_APPLICATIONS_ACTION = "appconfig:ListApplications"
# Fixo: mudar o id físico faria o CloudFormation substituir o recurso, isto é, implantar de novo.
INITIAL_DEPLOYMENT_PHYSICAL_ID = "prumo-initial-prompt-deployment"


class PromptConfig(Construct):
    """Application "prumo", Environment "prod" e o profile hospedado "prompt-version"."""

    def __init__(
        self, scope: Construct, construct_id: str, *, dev: bool, removal_policy: RemovalPolicy
    ) -> None:
        super().__init__(scope, construct_id)
        # Em dev a stack é descartável: pular a proteção de "configuração usada há pouco" deixa
        # o `cdk destroy` funcionar logo depois de um teste.
        protection = "BYPASS" if dev else "ACCOUNT_DEFAULT"
        self.application = appconfig.CfnApplication(
            self, "Application", name="prumo", description="Versão ativa do prompt do Prumo"
        )
        self.environment = appconfig.CfnEnvironment(
            self,
            "Environment",
            application_id=self.application_id,
            name="prod",
            deletion_protection_check=protection,
        )
        self.profile = self._profile(protection)
        self.strategy = self._all_at_once_strategy()
        self._initial_deployment(removal_policy)

    @property
    def application_id(self) -> str:
        return self.application.attr_application_id

    @property
    def environment_id(self) -> str:
        return self.environment.attr_environment_id

    @property
    def profile_id(self) -> str:
        return self.profile.attr_configuration_profile_id

    @property
    def strategy_id(self) -> str:
        return self.strategy.attr_id

    def _profile(self, protection: str) -> appconfig.CfnConfigurationProfile:
        return appconfig.CfnConfigurationProfile(
            self,
            "PromptVersionProfile",
            application_id=self.application_id,
            name="prompt-version",
            location_uri="hosted",
            type="AWS.Freeform",
            deletion_protection_check=protection,
            validators=[
                appconfig.CfnConfigurationProfile.ValidatorsProperty(
                    type="JSON_SCHEMA", content=json.dumps(PROMPT_DOCUMENT_SCHEMA)
                )
            ],
        )

    def _all_at_once_strategy(self) -> appconfig.CfnDeploymentStrategy:
        # Sem bake time de propósito: o rollback é decidido pela nossa Lambda a partir da faixa de
        # confiança (horas de dados), não por um alarme durante o bake. E enquanto um deployment
        # está "assando" o AppConfig recusa outro no mesmo environment, então um bake time
        # bloquearia justamente o rollback que precisa sair rápido.
        return appconfig.CfnDeploymentStrategy(
            self,
            "AllAtOnce",
            name="prumo-all-at-once",
            deployment_duration_in_minutes=0,
            final_bake_time_in_minutes=0,
            growth_factor=100,
            growth_type="LINEAR",
            replicate_to="NONE",
        )

    def _initial_deployment(self, removal_policy: RemovalPolicy) -> None:
        # O CloudFormation só publica a versão inicial. Depois disso a versão ativa é estado de
        # runtime (deploy/rollback pela API e pela Lambda). Um AWS::AppConfig::Deployment seria
        # recriado a cada mudança de propriedade (documento, estratégia, ids), e recriar um
        # deployment REIMPLANTA a v1 por cima do prompt em produção, desfazendo um rollback sem
        # ninguém pedir. O custom resource só chama StartDeployment no Create; no Update não
        # faz nada (e mantém o mesmo id físico, então o CloudFormation nunca o substitui).
        initial = appconfig.CfnHostedConfigurationVersion(
            self,
            "InitialVersion",
            application_id=self.application_id,
            configuration_profile_id=self.profile_id,
            content=json.dumps(INITIAL_DOCUMENT),
            content_type="application/json",
            description="Versão inicial do prompt",
        )
        cr.AwsCustomResource(
            self,
            "InitialDeployment",
            resource_type="Custom::PrumoInitialPromptDeployment",
            on_create=cr.AwsSdkCall(
                service="AppConfig",
                action="StartDeployment",
                parameters={
                    "ApplicationId": self.application_id,
                    "EnvironmentId": self.environment_id,
                    "ConfigurationProfileId": self.profile_id,
                    "ConfigurationVersion": initial.attr_version_number,
                    "DeploymentStrategyId": self.strategy_id,
                    "Description": "Publica a versão inicial do prompt",
                },
                physical_resource_id=cr.PhysicalResourceId.of(INITIAL_DEPLOYMENT_PHYSICAL_ID),
                # A resposta inteira pode passar do limite de 4 KB do CloudFormation.
                output_paths=["DeploymentNumber"],
            ),
            policy=cr.AwsCustomResourcePolicy.from_statements(
                [
                    iam.PolicyStatement(
                        actions=["appconfig:StartDeployment"], resources=self._deploy_arns()
                    )
                ]
            ),
            log_group=logs.LogGroup(
                self,
                "InitialDeploymentLogs",
                retention=LOG_RETENTION,
                removal_policy=removal_policy,
            ),
            install_latest_aws_sdk=False,
        )

    def grant_read(self, grantee: iam.IGrantable) -> None:
        """Leitura pela API de dados do AppConfig, só nesta configuração."""
        iam.Grant.add_to_principal(
            grantee=grantee, actions=list(READ_ACTIONS), resource_arns=[self._configuration_arn()]
        )

    def grant_deploy(self, grantee: iam.IGrantable) -> None:
        """Publicar uma versão nova e implantá-la (deploy de prompt e rollback)."""
        iam.Grant.add_to_principal(
            grantee=grantee, actions=list(RESOLVE_ACTIONS), resource_arns=[self._application_arn()]
        )
        iam.Grant.add_to_principal(
            grantee=grantee, actions=[LIST_APPLICATIONS_ACTION], resource_arns=["*"]
        )
        iam.Grant.add_to_principal(
            grantee=grantee, actions=list(DEPLOY_ACTIONS), resource_arns=self._deploy_arns()
        )
        iam.Grant.add_to_principal(
            grantee=grantee,
            actions=list(OPTIMISTIC_LOCK_ACTIONS),
            resource_arns=[
                self._environment_arn(),
                f"{self._environment_arn()}/deployment/*",
                self._profile_arn(),
                f"{self._profile_arn()}/hostedconfigurationversion/*",
            ],
        )

    def _deploy_arns(self) -> list[str]:
        return [
            self._application_arn(),
            self._environment_arn(),
            self._profile_arn(),
            f"{self._profile_arn()}/hostedconfigurationversion/*",
            self._arn(f"deploymentstrategy/{self.strategy_id}"),
        ]

    def _application_arn(self) -> str:
        return self._arn(f"application/{self.application_id}")

    def _environment_arn(self) -> str:
        return self._arn(f"application/{self.application_id}/environment/{self.environment_id}")

    def _configuration_arn(self) -> str:
        return f"{self._environment_arn()}/configuration/{self.profile_id}"

    def _profile_arn(self) -> str:
        return self._arn(
            f"application/{self.application_id}/configurationprofile/{self.profile_id}"
        )

    @staticmethod
    def _arn(resource: str) -> str:
        return f"arn:{Aws.PARTITION}:appconfig:{Aws.REGION}:{Aws.ACCOUNT_ID}:{resource}"

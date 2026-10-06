"""API Gateway HTTP API na frente da Lambda da API, com throttling, CORS restrito e access log."""

from __future__ import annotations

import json

from aws_cdk import Duration, RemovalPolicy
from aws_cdk import aws_apigatewayv2 as apigw
from aws_cdk import aws_apigatewayv2_integrations as integrations
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from constructs import Construct

from prumo_infra.config import LOG_RETENTION

ALLOWED_HEADERS = ["content-type", "x-prumo-token", "x-correlation-id"]
ALLOWED_METHODS = [apigw.CorsHttpMethod.GET, apigw.CorsHttpMethod.POST]
CORS_MAX_AGE = Duration.hours(1)
ACCESS_LOG_FORMAT = {
    "requestId": "$context.requestId",
    "sourceIp": "$context.identity.sourceIp",
    "requestTime": "$context.requestTime",
    "method": "$context.httpMethod",
    "route": "$context.routeKey",
    "status": "$context.status",
    "latencyMs": "$context.responseLatency",
    "responseLength": "$context.responseLength",
    "integrationError": "$context.integrationErrorMessage",
}


class PublicApi(Construct):
    """Rota $default em proxy para a Lambda (payload 2.0, que o Mangum entende)."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        handler: lambda_.IFunction,
        allowed_origins: tuple[str, ...],
        rate_limit: int,
        burst_limit: int,
        removal_policy: RemovalPolicy,
    ) -> None:
        super().__init__(scope, construct_id)
        self.http_api = apigw.HttpApi(
            self,
            "HttpApi",
            api_name="prumo",
            description="API do Prumo",
            # O stage padrão do L2 não aceita throttling; criamos o nosso logo abaixo.
            create_default_stage=False,
            default_integration=integrations.HttpLambdaIntegration(
                "LambdaIntegration",
                handler,
                payload_format_version=apigw.PayloadFormatVersion.VERSION_2_0,
            ),
            cors_preflight=apigw.CorsPreflightOptions(
                allow_origins=list(allowed_origins),
                allow_methods=ALLOWED_METHODS,
                allow_headers=ALLOWED_HEADERS,
                max_age=CORS_MAX_AGE,
            ),
        )
        self.stage = apigw.HttpStage(
            self,
            "DefaultStage",
            http_api=self.http_api,
            stage_name="$default",
            auto_deploy=True,
            # Teto de custo e de abuso: a rota pública chama o Bedrock a cada decisão.
            throttle=apigw.ThrottleSettings(rate_limit=rate_limit, burst_limit=burst_limit),
        )
        self._enable_access_logs(removal_policy)

    @property
    def url(self) -> str:
        return self.stage.url

    def _enable_access_logs(self, removal_policy: RemovalPolicy) -> None:
        log_group = logs.LogGroup(
            self, "AccessLogs", retention=LOG_RETENTION, removal_policy=removal_policy
        )
        cfn_stage = self.stage.node.default_child
        if not isinstance(cfn_stage, apigw.CfnStage):
            raise TypeError("o stage da HTTP API deveria ser um CfnStage")
        cfn_stage.access_log_settings = apigw.CfnStage.AccessLogSettingsProperty(
            destination_arn=log_group.log_group_arn, format=json.dumps(ACCESS_LOG_FORMAT)
        )

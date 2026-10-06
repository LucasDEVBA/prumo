"""Invariantes da PrumoStack verificados no template sintetizado (regressão vira teste vermelho)."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any

import pytest
from aws_cdk import assertions

from tests.conftest import REPO_ROOT, TemplateFactory

Match = assertions.Match
SCOPED_SERVICES = (
    "dynamodb:",
    "states:",
    "appconfig:",
    "bedrock:",
    "secretsmanager:",
    "cloudwatch:",
    "sqs:",
)
# Ações que o IAM não deixa restringir por ARN (Service Authorization Reference): só elas podem
# usar "*". Qualquer outra ação desses serviços com "*" é regressão.
NO_RESOURCE_LEVEL_ACTIONS = {"states:SendTaskSuccess", "appconfig:ListApplications"}
BEDROCK_CALLS_PER_REQUEST = 2  # classificador + juiz, em sequência
API_TIMEOUT_SLACK_SECONDS = 5
HTTP_API_MAX_LAMBDA_TIMEOUT = 29  # o HTTP API corta a integração em 30 s
HANDLERS = {
    "prumo.lambdas.api.handler",
    "prumo.lambdas.estimator.handler",
    "prumo.lambdas.rollback.handler",
    "prumo.lambdas.review_task.handler",
}


def _resources(template: assertions.Template, kind: str) -> dict[str, Any]:
    return dict(template.find_resources(kind))


def _policy_statements(template: assertions.Template) -> Iterator[dict[str, Any]]:
    for policy in _resources(template, "AWS::IAM::Policy").values():
        yield from policy["Properties"]["PolicyDocument"]["Statement"]
    for role in _resources(template, "AWS::IAM::Role").values():
        for inline in role["Properties"].get("Policies", []):
            yield from inline["PolicyDocument"]["Statement"]


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def _function_by_handler(template: assertions.Template, handler: str) -> dict[str, Any]:
    functions = _resources(template, "AWS::Lambda::Function")
    matches: list[dict[str, Any]] = [
        f for f in functions.values() if f["Properties"].get("Handler") == handler
    ]
    assert len(matches) == 1, f"esperava 1 função com handler {handler}"
    return matches[0]


def _statements_of_role(
    template: assertions.Template, role_logical_id: str
) -> list[dict[str, Any]]:
    statements: list[dict[str, Any]] = []
    for policy in _resources(template, "AWS::IAM::Policy").values():
        roles = policy["Properties"].get("Roles", [])
        if {"Ref": role_logical_id} in roles:
            statements.extend(policy["Properties"]["PolicyDocument"]["Statement"])
    return statements


def _actions_of(template: assertions.Template, handler: str) -> set[str]:
    role = _function_by_handler(template, handler)["Properties"]["Role"]["Fn::GetAtt"][0]
    return {
        action
        for statement in _statements_of_role(template, role)
        for action in _as_list(statement["Action"])
    }


def test_should_alarm_on_slo_breached_for_two_of_two_hourly_readings(
    template: assertions.Template,
) -> None:
    template.has_resource_properties(
        "AWS::CloudWatch::Alarm",
        {
            "Namespace": "Prumo",
            "MetricName": "SloBreached",
            "Dimensions": Match.absent(),
            "Statistic": "Maximum",
            "Period": 3600,
            "EvaluationPeriods": 2,
            "DatapointsToAlarm": 2,
            "Threshold": 1,
            "ComparisonOperator": "GreaterThanOrEqualToThreshold",
            "TreatMissingData": "notBreaching",
        },
    )


def test_should_route_slo_alarm_state_change_to_rollback_lambda(
    template: assertions.Template,
) -> None:
    rules = _resources(template, "AWS::Events::Rule")
    rollback_rules = [
        r for r in rules.values() if r["Properties"].get("EventPattern", {}).get("source")
    ]
    assert len(rollback_rules) == 1
    pattern = rollback_rules[0]["Properties"]["EventPattern"]
    assert pattern["detail-type"] == ["CloudWatch Alarm State Change"]
    assert pattern["detail"] == {"state": {"value": ["ALARM"]}}
    target_arn = rollback_rules[0]["Properties"]["Targets"][0]["Arn"]["Fn::GetAtt"][0]
    rollback = _function_by_handler(template, "prumo.lambdas.rollback.handler")
    functions = _resources(template, "AWS::Lambda::Function")
    assert functions[target_arn] == rollback
    alarm_id = pattern["resources"][0]["Fn::GetAtt"][0]
    alarm = _resources(template, "AWS::CloudWatch::Alarm")[alarm_id]
    assert alarm["Properties"]["MetricName"] == "SloBreached"


def _retry_attempts_of(template: assertions.Template, handler: str) -> int:
    function = _function_by_handler(template, handler)
    functions = _resources(template, "AWS::Lambda::Function")
    logical_id = next(name for name, f in functions.items() if f == function)
    configs = _resources(template, "AWS::Lambda::EventInvokeConfig").values()
    config = next(c for c in configs if c["Properties"]["FunctionName"] == {"Ref": logical_id})
    attempts: int = config["Properties"]["MaximumRetryAttempts"]
    return attempts


def test_should_configure_async_retries_of_scheduled_and_alarm_lambdas(
    template: assertions.Template,
) -> None:
    assert _retry_attempts_of(template, "prumo.lambdas.estimator.handler") == 1
    assert _retry_attempts_of(template, "prumo.lambdas.rollback.handler") == 2


def test_should_have_dead_man_switch_on_estimator_invocations(
    template: assertions.Template,
) -> None:
    template.has_resource_properties(
        "AWS::CloudWatch::Alarm",
        {
            "Namespace": "AWS/Lambda",
            "MetricName": "Invocations",
            "Period": 7200,
            "Threshold": 1,
            "ComparisonOperator": "LessThanThreshold",
            "TreatMissingData": "breaching",
            "AlarmActions": [{"Ref": Match.string_like_regexp("MonitoringAlerts")}],
        },
    )


def test_should_schedule_estimator_every_hour(template: assertions.Template) -> None:
    template.has_resource_properties(
        "AWS::Scheduler::Schedule", {"ScheduleExpression": "rate(1 hour)"}
    )


def test_should_wait_for_task_token_with_72h_timeout_and_catch(
    template: assertions.Template,
) -> None:
    machines = _resources(template, "AWS::StepFunctions::StateMachine")
    assert len(machines) == 1
    definition = json.loads(_render_definition(next(iter(machines.values()))))
    task = definition["States"]["AguardarRevisao"]
    assert task["Resource"].endswith(":states:::lambda:invoke.waitForTaskToken")
    assert task["TimeoutSeconds"] == 72 * 3600
    assert task["Parameters"]["Payload"] == {
        "decision_id.$": "$.decision_id",
        "task_token.$": "$$.Task.Token",
    }
    assert task["Catch"][0]["ErrorEquals"] == ["States.Timeout"]
    assert task["Catch"][0]["Next"] == "RevisaoExpirada"
    assert definition["States"]["RevisaoExpirada"]["Type"] in {"Succeed", "Pass"}


def _render_definition(machine: dict[str, Any]) -> str:
    """O DefinitionString vem como Fn::Join com ARNs em tokens; trocamos os tokens por texto."""
    parts = machine["Properties"]["DefinitionString"]["Fn::Join"][1]
    return "".join(p if isinstance(p, str) else "TOKEN" for p in parts)


def test_should_be_a_standard_state_machine(template: assertions.Template) -> None:
    template.has_resource_properties(
        "AWS::StepFunctions::StateMachine", {"StateMachineType": "STANDARD"}
    )


def test_should_throttle_api_stage(template: assertions.Template) -> None:
    template.has_resource_properties(
        "AWS::ApiGatewayV2::Stage",
        {
            "StageName": "$default",
            "DefaultRouteSettings": {"ThrottlingRateLimit": 10, "ThrottlingBurstLimit": 20},
            "AccessLogSettings": Match.object_like({"DestinationArn": Match.any_value()}),
        },
    )


def test_should_restrict_cors_without_wildcard(template: assertions.Template) -> None:
    api = next(iter(_resources(template, "AWS::ApiGatewayV2::Api").values()))
    origins = api["Properties"]["CorsConfiguration"]["AllowOrigins"]
    assert origins == ["http://localhost:8000"]
    assert all("*" not in origin for origin in origins)


def test_should_honor_context_overrides(make_template: TemplateFactory) -> None:
    template = make_template(
        allowedOrigins="https://prumo.example.com,https://admin.example.com",
        apiRateLimit="5",
        apiBurstLimit="7",
    )
    api = next(iter(_resources(template, "AWS::ApiGatewayV2::Api").values()))
    assert api["Properties"]["CorsConfiguration"]["AllowOrigins"] == [
        "https://prumo.example.com",
        "https://admin.example.com",
    ]
    template.has_resource_properties(
        "AWS::ApiGatewayV2::Stage",
        {"DefaultRouteSettings": {"ThrottlingRateLimit": 5, "ThrottlingBurstLimit": 7}},
    )


def test_should_never_use_wildcard_resource_for_scoped_services(
    template: assertions.Template,
) -> None:
    offenders = [
        action
        for statement in _policy_statements(template)
        if "*" in _as_list(statement["Resource"])
        for action in _as_list(statement["Action"])
        if str(action).startswith(SCOPED_SERVICES) and action not in NO_RESOURCE_LEVEL_ACTIONS
    ]
    assert offenders == []


def test_should_enable_point_in_time_recovery_on_table(template: assertions.Template) -> None:
    template.has_resource_properties(
        "AWS::DynamoDB::Table",
        {
            "BillingMode": "PAY_PER_REQUEST",
            "KeySchema": [
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            "PointInTimeRecoverySpecification": {"PointInTimeRecoveryEnabled": True},
        },
    )


def test_should_have_sparse_review_queue_index(template: assertions.Template) -> None:
    template.has_resource_properties(
        "AWS::DynamoDB::Table",
        {
            "GlobalSecondaryIndexes": [
                {
                    "IndexName": "gsi1",
                    "KeySchema": [
                        {"AttributeName": "gsi1pk", "KeyType": "HASH"},
                        {"AttributeName": "gsi1sk", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ]
        },
    )


def test_should_let_api_query_the_review_queue_index(template: assertions.Template) -> None:
    role = _function_by_handler(template, "prumo.lambdas.api.handler")["Properties"]["Role"]
    statements = _statements_of_role(template, role["Fn::GetAtt"][0])
    query = [s for s in statements if "dynamodb:Query" in _as_list(s["Action"])]
    assert "/index/*" in json.dumps(query)


def test_should_retain_table_by_default_and_destroy_in_dev(make_template: TemplateFactory) -> None:
    prod = make_template()
    prod.has_resource("AWS::DynamoDB::Table", {"DeletionPolicy": "Retain"})
    dev = make_template(dev="true")
    dev.has_resource("AWS::DynamoDB::Table", {"DeletionPolicy": "Delete"})


def test_should_create_four_python_arm_lambdas_with_tracing(template: assertions.Template) -> None:
    functions = _resources(template, "AWS::Lambda::Function")
    prumo = {name: f for name, f in functions.items() if f["Properties"].get("Handler") in HANDLERS}
    assert {f["Properties"]["Handler"] for f in prumo.values()} == HANDLERS
    for function in prumo.values():
        props = function["Properties"]
        assert props["Runtime"] == "python3.12"
        assert props["Architectures"] == ["arm64"]
        assert props["TracingConfig"] == {"Mode": "Active"}
        assert "LoggingConfig" in props


def test_should_enable_rollback_switch_in_every_function(template: assertions.Template) -> None:
    for handler in ("prumo.lambdas.estimator.handler", "prumo.lambdas.rollback.handler"):
        variables = _function_by_handler(template, handler)["Properties"]["Environment"]
        assert variables["Variables"]["PRUMO_AUTO_ROLLBACK"] == "true", handler
    estimator = _function_by_handler(template, "prumo.lambdas.estimator.handler")
    variables = estimator["Properties"]["Environment"]["Variables"]
    assert variables["PRUMO_PROVIDER"] == "bedrock"
    assert variables["PRUMO_STORAGE"] == "dynamodb"
    assert variables["PRUMO_PROMPT_BACKEND"] == "appconfig"
    assert variables["PRUMO_METRICS_NAMESPACE"] == "Prumo"


def test_should_set_log_retention_on_every_log_group(template: assertions.Template) -> None:
    groups = _resources(template, "AWS::Logs::LogGroup")
    assert groups
    assert all(g["Properties"].get("RetentionInDays") == 30 for g in groups.values())


def test_should_grant_appconfig_deploy_only_to_publishers(
    template: assertions.Template,
) -> None:
    deploy = {"appconfig:CreateHostedConfigurationVersion", "appconfig:StartDeployment"}
    assert deploy <= _actions_of(template, "prumo.lambdas.api.handler")
    assert deploy <= _actions_of(template, "prumo.lambdas.rollback.handler")
    # O estimador publica porque é a rede de segurança horária do rollback.
    assert deploy <= _actions_of(template, "prumo.lambdas.estimator.handler")
    assert not deploy & _actions_of(template, "prumo.lambdas.review_task.handler")


def test_should_grant_bedrock_and_review_apis_only_where_used(
    template: assertions.Template,
) -> None:
    api_actions = _actions_of(template, "prumo.lambdas.api.handler")
    assert {a for a in api_actions if a.startswith("states:")} == {
        "states:StartExecution",
        "states:SendTaskSuccess",
    }
    assert "bedrock:InvokeModel" in api_actions
    review_actions = _actions_of(template, "prumo.lambdas.review_task.handler")
    assert {a for a in review_actions if a.startswith("states:")} == {"states:SendTaskSuccess"}
    for handler in ("prumo.lambdas.estimator.handler", "prumo.lambdas.rollback.handler"):
        actions = _actions_of(template, handler)
        assert not any(a.startswith(("bedrock:", "states:")) for a in actions), handler
    for handler in HANDLERS - {"prumo.lambdas.api.handler"}:
        assert not any(a.startswith("bedrock:") for a in _actions_of(template, handler))


def test_should_scope_start_execution_to_review_state_machine(
    template: assertions.Template,
) -> None:
    machine_id = next(iter(_resources(template, "AWS::StepFunctions::StateMachine")))
    starts = [
        s for s in _policy_statements(template) if "states:StartExecution" in _as_list(s["Action"])
    ]
    assert starts
    assert all(s["Resource"] == {"Ref": machine_id} for s in starts)


def _bedrock_statements(template: assertions.Template) -> list[dict[str, Any]]:
    return [s for s in _policy_statements(template) if "bedrock:" in json.dumps(s["Action"])]


def test_should_scope_bedrock_to_configured_models(make_template: TemplateFactory) -> None:
    template = make_template(
        classifierModel="amazon.nova-micro-v1:0", bedrockGuardrailId="abc123def456"
    )
    rendered = json.dumps(_bedrock_statements(template))
    assert "foundation-model/amazon.nova-micro-v1:0" in rendered
    assert "inference-profile/us.anthropic.claude-haiku-4-5-20251001-v1:0" in rendered
    assert "guardrail/abc123def456" in rendered
    assert "bedrock:ApplyGuardrail" in rendered


def _render(value: Any) -> str:
    """Achata Fn::Join/Ref em texto ("${AWS::Region}"), para comparar ARNs montados com tokens."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and "Fn::Join" in value:
        separator, parts = value["Fn::Join"]
        return str(separator).join(_render(part) for part in parts)
    if isinstance(value, dict) and "Ref" in value:
        return "${" + str(value["Ref"]) + "}"
    if isinstance(value, dict) and "Fn::GetAtt" in value:
        return "${" + ".".join(value["Fn::GetAtt"]) + "}"
    return json.dumps(value, sort_keys=True)


def test_should_allow_any_region_model_only_through_the_inference_profile(
    template: assertions.Template,
) -> None:
    any_region = [
        s
        for s in _bedrock_statements(template)
        if ":bedrock:*::foundation-model/" in _render(s["Resource"])
    ]
    # Padrão: classificador e juiz são profiles "us.", cada um com o seu statement condicionado.
    assert len(any_region) == 2
    for statement in any_region:
        base_model = _render(statement["Resource"]).split("foundation-model/")[1]
        profile = _render(statement["Condition"]["StringEquals"]["bedrock:InferenceProfileArn"])
        assert re.fullmatch(
            r"arn:\$\{AWS::Partition\}:bedrock:\$\{AWS::Region\}:\$\{AWS::AccountId\}"
            rf":inference-profile/[a-z-]+\.{re.escape(base_model)}",
            profile,
        ), profile


def test_should_configure_appconfig_without_bake_time(template: assertions.Template) -> None:
    template.has_resource_properties("AWS::AppConfig::Application", {"Name": "prumo"})
    template.has_resource_properties("AWS::AppConfig::Environment", {"Name": "prod"})
    template.has_resource_properties(
        "AWS::AppConfig::ConfigurationProfile",
        {
            "Name": "prompt-version",
            "LocationUri": "hosted",
            "Validators": [Match.object_like({"Type": "JSON_SCHEMA"})],
        },
    )
    template.has_resource_properties(
        "AWS::AppConfig::DeploymentStrategy",
        {"DeploymentDurationInMinutes": 0, "FinalBakeTimeInMinutes": 0, "GrowthFactor": 100},
    )
    # Nenhum AWS::AppConfig::Deployment: ele seria recriado (e reimplantaria a v1) a cada mudança.
    template.resource_count_is("AWS::AppConfig::Deployment", 0)


def test_should_publish_initial_prompt_document(template: assertions.Template) -> None:
    version = next(
        iter(_resources(template, "AWS::AppConfig::HostedConfigurationVersion").values())
    )
    assert json.loads(version["Properties"]["Content"]) == {"active": "v1", "history": ["v1"]}
    assert version["Properties"]["ContentType"] == "application/json"


def test_should_mirror_prompt_version_pattern_from_domain(template: assertions.Template) -> None:
    models = (REPO_ROOT / "src" / "prumo" / "domain" / "models.py").read_text()
    match = re.search(r'PROMPT_VERSION_PATTERN = r"([^"]+)"', models)
    assert match is not None
    profile = next(iter(_resources(template, "AWS::AppConfig::ConfigurationProfile").values()))
    schema = json.loads(profile["Properties"]["Validators"][0]["Content"])
    assert schema["properties"]["active"]["pattern"] == match.group(1)


def test_should_pass_only_the_admin_token_secret_arn_to_the_api(
    template: assertions.Template,
) -> None:
    secret_id = next(iter(_resources(template, "AWS::SecretsManager::Secret")))
    api = _function_by_handler(template, "prumo.lambdas.api.handler")
    variables = api["Properties"]["Environment"]["Variables"]
    assert "PRUMO_ADMIN_TOKEN" not in variables
    assert variables["PRUMO_ADMIN_TOKEN_SECRET_ARN"] == {"Ref": secret_id}
    reads = [
        s
        for s in _statements_of_role(template, api["Properties"]["Role"]["Fn::GetAtt"][0])
        if "secretsmanager:GetSecretValue" in _as_list(s["Action"])
    ]
    assert [s["Resource"] for s in reads] == [{"Ref": secret_id}]


def test_should_never_resolve_a_secret_into_any_environment_variable(
    template: assertions.Template,
) -> None:
    # Valor em env var aparece no GetFunctionConfiguration e congela até o próximo deploy.
    for name, function in _resources(template, "AWS::Lambda::Function").items():
        variables = function["Properties"].get("Environment", {}).get("Variables", {})
        rendered = json.dumps(variables)
        assert "resolve:secretsmanager" not in rendered, name
        assert "SecretString" not in rendered, name
    for name, function in _resources(template, "AWS::Lambda::Function").items():
        role = function["Properties"]["Role"]["Fn::GetAtt"][0]
        if function["Properties"].get("Handler") == "prumo.lambdas.api.handler":
            continue
        actions = {a for s in _statements_of_role(template, role) for a in _as_list(s["Action"])}
        assert not any(a.startswith("secretsmanager:") for a in actions), name


def test_should_create_budget_only_when_email_given(make_template: TemplateFactory) -> None:
    make_template().resource_count_is("AWS::Budgets::Budget", 0)
    with_budget = make_template(budgetEmail="financeiro@example.com", budgetUsd="30")
    with_budget.has_resource_properties(
        "AWS::Budgets::Budget",
        {
            "Budget": Match.object_like({"BudgetLimit": {"Amount": 30, "Unit": "USD"}}),
            "NotificationsWithSubscribers": [
                Match.object_like({"Notification": Match.object_like({"Threshold": 40})}),
                Match.object_like({"Notification": Match.object_like({"Threshold": 100})}),
            ],
        },
    )


def test_should_subscribe_alert_email_when_given(make_template: TemplateFactory) -> None:
    make_template().resource_count_is("AWS::SNS::Subscription", 0)
    make_template(alertEmail="oncall@example.com").has_resource_properties(
        "AWS::SNS::Subscription", {"Protocol": "email", "Endpoint": "oncall@example.com"}
    )


def test_should_export_api_url_table_and_state_machine(template: assertions.Template) -> None:
    outputs = template.find_outputs("*")
    assert {"ApiUrl", "DecisionsTableName", "ReviewStateMachineArn"} <= set(outputs)


@pytest.mark.parametrize(
    ("context", "message"),
    [
        ({"allowedOrigins": "*"}, "curinga"),
        ({"allowedOrigins": "http://evil.example.com"}, "origem CORS inválida"),
        ({"apiRateLimit": "0"}, "apiRateLimit"),
        ({"budgetEmail": "não-é-email"}, "budgetEmail"),
        ({"dev": "talvez"}, "dev"),
    ],
)
def test_should_fail_fast_on_invalid_context(
    make_template: TemplateFactory, context: dict[str, str], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        make_template(**context)


def _connect_timeout_seconds() -> int:
    """Lido do adaptador, para o teste quebrar se alguém mudar o connect timeout lá."""
    common = (REPO_ROOT / "src" / "prumo" / "adapters" / "aws" / "_common.py").read_text()
    match = re.search(r"^CONNECT_TIMEOUT_SECONDS = (\d+)$", common, re.MULTILINE)
    assert match is not None, "CONNECT_TIMEOUT_SECONDS sumiu de adapters/aws/_common.py"
    return int(match.group(1))


def test_should_fit_bedrock_worst_case_inside_api_lambda_timeout(
    template: assertions.Template,
) -> None:
    props = _function_by_handler(template, "prumo.lambdas.api.handler")["Properties"]
    variables = props["Environment"]["Variables"]
    attempts = int(variables["PRUMO_BEDROCK_MAX_ATTEMPTS"])
    read_timeout = int(variables["PRUMO_BEDROCK_TIMEOUT_SECONDS"])
    # Pior caso de uma requisição: classificador e juiz em sequência, cada um esgotando todas as
    # tentativas com connect + read cheios. A folga cobre cold start e DynamoDB/AppConfig/SFN.
    worst_case = BEDROCK_CALLS_PER_REQUEST * attempts * (_connect_timeout_seconds() + read_timeout)
    assert props["Timeout"] <= HTTP_API_MAX_LAMBDA_TIMEOUT
    assert worst_case + API_TIMEOUT_SLACK_SECONDS < props["Timeout"], (
        f"pior caso de {worst_case} s + {API_TIMEOUT_SLACK_SECONDS} s de folga não cabe nos "
        f"{props['Timeout']} s da Lambda: o API Gateway cortaria antes do erro tratado e o "
        "cliente veria 503 sem saber se a decisão foi gravada"
    )


def _alerts_topic_id(template: assertions.Template) -> str:
    topics = _resources(template, "AWS::SNS::Topic")
    assert len(topics) == 1
    return next(iter(topics))


def _alarm_with_metric(
    template: assertions.Template, namespace: str, metric: str
) -> dict[str, Any]:
    alarms: list[dict[str, Any]] = [
        a["Properties"]
        for a in _resources(template, "AWS::CloudWatch::Alarm").values()
        if a["Properties"].get("Namespace") == namespace
        and a["Properties"].get("MetricName") == metric
    ]
    assert len(alarms) == 1, f"esperava 1 alarme em {namespace}/{metric}"
    return alarms[0]


def test_should_let_cloudwatch_alarms_publish_to_the_alerts_topic(
    template: assertions.Template,
) -> None:
    topic_id = _alerts_topic_id(template)
    policies = _resources(template, "AWS::SNS::TopicPolicy").values()
    statements = [
        s
        for p in policies
        if p["Properties"]["Topics"] == [{"Ref": topic_id}]
        for s in p["Properties"]["PolicyDocument"]["Statement"]
    ]
    # A TopicPolicy do enforce_ssl substitui a padrão: sem um Allow, nenhum alarme publica.
    allows = [s for s in statements if s["Effect"] == "Allow"]
    assert len(allows) == 1
    allow = allows[0]
    assert allow["Principal"] == {"Service": "cloudwatch.amazonaws.com"}
    assert allow["Action"] == "sns:Publish"
    assert allow["Resource"] == {"Ref": topic_id}
    assert _render(allow["Condition"]["ArnLike"]["aws:SourceArn"]) == (
        "arn:${AWS::Partition}:cloudwatch:${AWS::Region}:${AWS::AccountId}:alarm:*"
    )
    assert allow["Condition"]["StringEquals"] == {"aws:SourceAccount": {"Ref": "AWS::AccountId"}}
    assert any(
        s["Effect"] == "Deny" and s["Condition"] == {"Bool": {"aws:SecureTransport": "false"}}
        for s in statements
    )


def test_should_send_every_alarm_to_the_alerts_topic(template: assertions.Template) -> None:
    topic_id = _alerts_topic_id(template)
    alarms = _resources(template, "AWS::CloudWatch::Alarm")
    silent = [
        name
        for name, alarm in alarms.items()
        if {"Ref": topic_id} not in alarm["Properties"].get("AlarmActions", [])
    ]
    assert silent == []


def test_should_not_let_any_function_change_alarm_state(template: assertions.Template) -> None:
    """O re-arme manual do alarme criaria um ciclo ALARM/OK; quem cobre é o estimador."""
    actions = {
        action
        for statement in _policy_statements(template)
        for action in _as_list(statement["Action"])
    }
    assert "cloudwatch:SetAlarmState" not in actions
    rollback = _function_by_handler(template, "prumo.lambdas.rollback.handler")
    assert "PRUMO_QUALITY_ALARM_NAME" not in rollback["Properties"]["Environment"]["Variables"]


def _dead_letter_queue_id(template: assertions.Template) -> str:
    queues = _resources(template, "AWS::SQS::Queue")
    assert len(queues) == 1
    return next(iter(queues))


def test_should_keep_failed_rollback_events_in_an_encrypted_tls_only_queue(
    template: assertions.Template,
) -> None:
    queue_id = _dead_letter_queue_id(template)
    queue = _resources(template, "AWS::SQS::Queue")[queue_id]["Properties"]
    assert queue["SqsManagedSseEnabled"] is True
    assert queue["MessageRetentionPeriod"] == 14 * 24 * 3600
    policy = next(
        p["Properties"]["PolicyDocument"]["Statement"]
        for p in _resources(template, "AWS::SQS::QueuePolicy").values()
        if p["Properties"]["Queues"] == [{"Ref": queue_id}]
    )
    assert any(
        s["Effect"] == "Deny" and s["Condition"] == {"Bool": {"aws:SecureTransport": "false"}}
        for s in policy
    )
    queue_arn = {"Fn::GetAtt": [queue_id, "Arn"]}
    rule = next(
        r["Properties"]
        for r in _resources(template, "AWS::Events::Rule").values()
        if r["Properties"].get("EventPattern", {}).get("source") == ["aws.cloudwatch"]
    )
    assert rule["Targets"][0]["DeadLetterConfig"] == {"Arn": queue_arn}
    rollback = _function_by_handler(template, "prumo.lambdas.rollback.handler")
    rollback_id = next(
        n for n, f in _resources(template, "AWS::Lambda::Function").items() if f == rollback
    )
    invoke_config = next(
        c["Properties"]
        for c in _resources(template, "AWS::Lambda::EventInvokeConfig").values()
        if c["Properties"]["FunctionName"] == {"Ref": rollback_id}
    )
    assert invoke_config["DestinationConfig"] == {"OnFailure": {"Destination": queue_arn}}


def test_should_alarm_when_a_rollback_event_lands_in_the_dead_letter_queue(
    template: assertions.Template,
) -> None:
    alarm = _alarm_with_metric(template, "AWS/SQS", "ApproximateNumberOfMessagesVisible")
    assert alarm["Dimensions"] == [
        {
            "Name": "QueueName",
            "Value": {"Fn::GetAtt": [_dead_letter_queue_id(template), "QueueName"]},
        }
    ]
    assert alarm["Threshold"] == 1
    assert alarm["ComparisonOperator"] == "GreaterThanOrEqualToThreshold"


def test_should_not_reserve_concurrency(template: assertions.Template) -> None:
    # Conta nova tem cota de 10 execuções e a AWS recusa reservar abaixo do mínimo livre: o
    # deploy quebraria. Quem serializa o rollback é o compare-and-swap do PromptStore.
    functions = _resources(template, "AWS::Lambda::Function")
    reserved = [
        n for n, f in functions.items() if "ReservedConcurrentExecutions" in f["Properties"]
    ]
    assert reserved == []


def test_should_alarm_on_review_workflow_failed_log_event(template: assertions.Template) -> None:
    api = _function_by_handler(template, "prumo.lambdas.api.handler")
    api_log_group = api["Properties"]["LoggingConfig"]["LogGroup"]
    filters = list(_resources(template, "AWS::Logs::MetricFilter").values())
    assert len(filters) == 1
    props = filters[0]["Properties"]
    assert props["LogGroupName"] == api_log_group
    assert props["FilterPattern"] == '{ $.action = "review_workflow_failed" }'
    transformation = props["MetricTransformations"][0]
    assert transformation["MetricNamespace"] == "Prumo"
    assert transformation["MetricValue"] == "1"
    alarm = _alarm_with_metric(template, "Prumo", transformation["MetricName"])
    assert alarm["Statistic"] == "Sum"
    assert alarm["Threshold"] == 1


def test_should_retry_failed_review_task_but_not_the_72h_timeout(
    template: assertions.Template,
) -> None:
    machine = next(iter(_resources(template, "AWS::StepFunctions::StateMachine").values()))
    task = json.loads(_render_definition(machine))["States"]["AguardarRevisao"]
    task_failed = [r for r in task["Retry"] if r["ErrorEquals"] == ["States.TaskFailed"]]
    assert task_failed == [
        {
            "ErrorEquals": ["States.TaskFailed"],
            "IntervalSeconds": 2,
            "MaxAttempts": 3,
            "BackoffRate": 2,
        }
    ]
    assert all("States.Timeout" not in r["ErrorEquals"] for r in task["Retry"])


def test_should_alarm_on_failed_review_executions_and_review_task_errors(
    template: assertions.Template,
) -> None:
    machine_id = next(iter(_resources(template, "AWS::StepFunctions::StateMachine")))
    executions = _alarm_with_metric(template, "AWS/States", "ExecutionsFailed")
    assert executions["Dimensions"] == [{"Name": "StateMachineArn", "Value": {"Ref": machine_id}}]
    review_task = _function_by_handler(template, "prumo.lambdas.review_task.handler")
    review_task_id = next(
        n for n, f in _resources(template, "AWS::Lambda::Function").items() if f == review_task
    )
    errors = [
        a["Properties"]
        for a in _resources(template, "AWS::CloudWatch::Alarm").values()
        if a["Properties"].get("MetricName") == "Errors"
        and a["Properties"].get("Dimensions")
        == [{"Name": "FunctionName", "Value": {"Ref": review_task_id}}]
    ]
    assert len(errors) == 1


def test_should_expire_decisions_after_180_days(template: assertions.Template) -> None:
    template.has_resource_properties(
        "AWS::DynamoDB::Table",
        {"TimeToLiveSpecification": {"AttributeName": "expires_at", "Enabled": True}},
    )
    for handler in HANDLERS:
        variables = _function_by_handler(template, handler)["Properties"]["Environment"]
        assert variables["Variables"]["PRUMO_DECISION_TTL_DAYS"] == "180", handler


def test_should_point_lambdas_to_the_stack_deployment_strategy(
    template: assertions.Template,
) -> None:
    strategy_id = next(iter(_resources(template, "AWS::AppConfig::DeploymentStrategy")))
    for handler in HANDLERS:
        variables = _function_by_handler(template, handler)["Properties"]["Environment"]
        assert variables["Variables"]["PRUMO_APPCONFIG_DEPLOYMENT_STRATEGY"] == {
            "Fn::GetAtt": [strategy_id, "Id"]
        }, handler


def test_should_grant_optimistic_lock_reads_only_to_publishers(
    template: assertions.Template,
) -> None:
    lock = {"appconfig:ListDeployments", "appconfig:GetHostedConfigurationVersion"}
    assert lock <= _actions_of(template, "prumo.lambdas.api.handler")
    assert lock <= _actions_of(template, "prumo.lambdas.rollback.handler")
    assert lock <= _actions_of(template, "prumo.lambdas.estimator.handler")
    assert "appconfig:ListHostedConfigurationVersions" not in _actions_of(
        template, "prumo.lambdas.api.handler"
    )
    assert not lock & _actions_of(template, "prumo.lambdas.review_task.handler")
    application_id = next(iter(_resources(template, "AWS::AppConfig::Application")))
    for statement in _policy_statements(template):
        if lock & set(_as_list(statement["Action"])):
            for resource in _as_list(statement["Resource"]):
                rendered = _render(resource)
                assert f":application/${{{application_id}.ApplicationId}}/" in rendered, rendered


# Travas do deploy inicial do AppConfig. Estes ids e propriedades NÃO podem mudar depois do
# primeiro deploy: o CloudFormation trataria a mudança como recurso novo, e criar o recurso de
# deployment de novo REIMPLANTA a v1 por cima do prompt em produção (desfazendo deploys e
# rollbacks feitos em runtime, sem ninguém pedir). Se a mudança for intencional, faça a troca da
# versão ativa pela API e só então ajuste o teste, sabendo o que vai acontecer.
INITIAL_DEPLOYMENT_RISK = (
    "mudar o deploy inicial do AppConfig reimplanta a v1 em produção; leia o comentário acima "
    "de INITIAL_DEPLOYMENT_RISK em tests/test_stack.py antes de alterar"
)
LOCKED_APPCONFIG_LOGICAL_IDS = {
    "PromptsInitialVersion12050A23": "AWS::AppConfig::HostedConfigurationVersion",
    "PromptsAllAtOnce9FFF4487": "AWS::AppConfig::DeploymentStrategy",
    "PromptsInitialDeployment1DE2DBA9": "Custom::PrumoInitialPromptDeployment",
}


def test_should_keep_appconfig_initial_deployment_logical_ids(
    template: assertions.Template,
) -> None:
    resources = template.to_json()["Resources"]
    for logical_id, kind in LOCKED_APPCONFIG_LOGICAL_IDS.items():
        assert resources.get(logical_id, {}).get("Type") == kind, (
            f"{logical_id}: {INITIAL_DEPLOYMENT_RISK}"
        )


def test_should_start_initial_deployment_only_on_create(template: assertions.Template) -> None:
    resource = template.to_json()["Resources"]["PromptsInitialDeployment1DE2DBA9"]
    props = resource["Properties"]
    assert "Update" not in props, INITIAL_DEPLOYMENT_RISK
    assert "Delete" not in props, INITIAL_DEPLOYMENT_RISK
    create = json.loads(_render(props["Create"]))
    assert create["service"] == "AppConfig"
    assert create["action"] == "StartDeployment"
    assert create["physicalResourceId"] == {"id": "prumo-initial-prompt-deployment"}, (
        INITIAL_DEPLOYMENT_RISK
    )
    assert create["parameters"]["ConfigurationVersion"] == (
        "${PromptsInitialVersion12050A23.VersionNumber}"
    )
    assert create["parameters"]["DeploymentStrategyId"] == "${PromptsAllAtOnce9FFF4487.Id}"


def test_should_keep_initial_prompt_document_and_strategy(template: assertions.Template) -> None:
    resources = template.to_json()["Resources"]
    version = resources["PromptsInitialVersion12050A23"]["Properties"]
    assert json.loads(version["Content"]) == {"active": "v1", "history": ["v1"]}, (
        INITIAL_DEPLOYMENT_RISK
    )
    strategy = resources["PromptsAllAtOnce9FFF4487"]["Properties"]
    assert strategy == {
        "Name": "prumo-all-at-once",
        "DeploymentDurationInMinutes": 0,
        "FinalBakeTimeInMinutes": 0,
        "GrowthFactor": 100,
        "GrowthType": "LINEAR",
        "ReplicateTo": "NONE",
    }, INITIAL_DEPLOYMENT_RISK

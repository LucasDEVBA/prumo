"""Configuração por variáveis de ambiente (prefixo PRUMO_). Nada de segredo no código."""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from prumo.stats.ppi import EstimationMethod


class Provider(StrEnum):
    SIMULATED = "simulated"
    BEDROCK = "bedrock"


class Storage(StrEnum):
    MEMORY = "memory"
    DYNAMODB = "dynamodb"


class PromptBackend(StrEnum):
    LOCAL = "local"
    APPCONFIG = "appconfig"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PRUMO_", env_file=".env", extra="ignore")

    provider: Provider = Provider.SIMULATED
    storage: Storage = Storage.MEMORY
    prompt_backend: PromptBackend = PromptBackend.LOCAL

    slo_target: float = Field(default=0.85, gt=0, lt=1)
    alpha: float = Field(default=0.05, gt=0, lt=0.5)
    min_labels: int = Field(default=30, ge=2)
    breaches_to_rollback: int = Field(default=2, ge=1)
    method: EstimationMethod = EstimationMethod.PPI_CS
    auto_rollback: bool = True

    review_rate: float = Field(default=0.05, ge=0, le=1)
    """Fração das decisões sorteadas para revisão humana."""

    seed: int = 2026
    simulation_leads_per_hour: int = Field(default=400, ge=10, le=5000)
    simulation_warmup_hours: int = Field(default=30, ge=0, le=500)

    admin_token: SecretStr | None = None
    """Token exigido no header X-Prumo-Token nas rotas protegidas (uso local)."""
    admin_token_secret_arn: str | None = None
    """Na AWS: ARN do segredo no Secrets Manager, lido no cold start (nunca em env var)."""

    aws_region: str = "us-east-1"
    bedrock_classifier_model: str = "us.amazon.nova-lite-v1:0"
    bedrock_judge_model: str = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    bedrock_guardrail_id: str | None = None
    bedrock_guardrail_version: str = "DRAFT"
    bedrock_timeout_seconds: int = Field(default=8, ge=1, le=120)
    bedrock_max_attempts: int = Field(default=2, ge=1, le=5)
    """Classificador e juiz rodam em sequência dentro dos 29 s do API Gateway."""

    decisions_table: str = "prumo-decisions"
    appconfig_application: str = "prumo"
    appconfig_environment: str = "prod"
    appconfig_profile: str = "prompt-version"
    appconfig_deployment_strategy: str = "AppConfig.AllAtOnce"
    decision_ttl_days: int = Field(default=180, ge=1, le=3650)
    """Texto do lead é dado pessoal: expira no DynamoDB (as somas da estimativa ficam)."""
    review_state_machine_arn: str | None = None

    metrics_namespace: str = "Prumo"
    log_level: str = "INFO"

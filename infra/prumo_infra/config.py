"""Parâmetros da stack lidos do context do CDK (`-c chave=valor` ou cdk.json), validados cedo.

Valores errados quebram o `cdk synth` com mensagem clara, em vez de virar um deploy quebrado.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from aws_cdk import RemovalPolicy
from aws_cdk import aws_logs as logs
from constructs import Node

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LAMBDA_ASSET = REPO_ROOT / "build" / "lambda"
DEFAULT_ALLOWED_ORIGINS = ("http://localhost:8000",)
DEFAULT_CLASSIFIER_MODEL = "us.amazon.nova-lite-v1:0"
DEFAULT_JUDGE_MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
DEFAULT_BUDGET_USD = 25.0
DEFAULT_API_RATE_LIMIT = 10
DEFAULT_API_BURST_LIMIT = 20
LOG_RETENTION = logs.RetentionDays.ONE_MONTH

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_MODEL_ID = re.compile(r"^[a-z0-9][a-z0-9.\-:]{2,127}$")
_GUARDRAIL_ID = re.compile(r"^[a-z0-9]{1,64}$")
_GUARDRAIL_VERSION = re.compile(r"^(DRAFT|[1-9][0-9]{0,7})$")
_ORIGIN = re.compile(r"^(https://[a-z0-9.\-]+(:[0-9]{1,5})?|http://localhost(:[0-9]{1,5})?)$")
_TRUE = {"1", "true", "yes", "sim"}
_FALSE = {"", "0", "false", "no", "nao", "não"}


class ConfigError(ValueError):
    """Parâmetro de context inválido: o synth para aqui, antes de qualquer recurso existir."""


@dataclass(frozen=True, slots=True)
class StackConfig:
    """Tudo o que muda entre contas/ambientes. O resto da stack é fixo e revisado em código."""

    dev: bool = False
    alert_email: str | None = None
    budget_email: str | None = None
    budget_usd: float = DEFAULT_BUDGET_USD
    allowed_origins: tuple[str, ...] = DEFAULT_ALLOWED_ORIGINS
    api_rate_limit: int = DEFAULT_API_RATE_LIMIT
    api_burst_limit: int = DEFAULT_API_BURST_LIMIT
    classifier_model: str = DEFAULT_CLASSIFIER_MODEL
    judge_model: str = DEFAULT_JUDGE_MODEL
    guardrail_id: str | None = None
    guardrail_version: str = "DRAFT"
    lambda_asset_path: Path = DEFAULT_LAMBDA_ASSET

    def __post_init__(self) -> None:
        _check_email("alertEmail", self.alert_email)
        _check_email("budgetEmail", self.budget_email)
        _check(self.budget_usd > 0, "budgetUsd deve ser maior que zero")
        _check(self.api_rate_limit > 0, "apiRateLimit deve ser maior que zero")
        _check(self.api_burst_limit >= self.api_rate_limit, "apiBurstLimit < apiRateLimit")
        _check_origins(self.allowed_origins)
        for key, model in (
            ("classifierModel", self.classifier_model),
            ("judgeModel", self.judge_model),
        ):
            _check(bool(_MODEL_ID.match(model)), f"{key} inválido: {model!r}")
        if self.guardrail_id is not None:
            _check(bool(_GUARDRAIL_ID.match(self.guardrail_id)), "bedrockGuardrailId inválido")
        _check(
            bool(_GUARDRAIL_VERSION.match(self.guardrail_version)),
            "bedrockGuardrailVersion deve ser DRAFT ou um número",
        )

    @property
    def removal_policy(self) -> RemovalPolicy:
        """Produção guarda os dados ao destruir a stack; o modo dev apaga tudo."""
        return RemovalPolicy.DESTROY if self.dev else RemovalPolicy.RETAIN

    @classmethod
    def from_context(cls, node: Node) -> StackConfig:
        """Lê e valida o context. Chaves ausentes ficam com o padrão documentado em DEPLOY.md."""
        reader = _ContextReader(node)
        return cls(
            dev=reader.boolean("dev"),
            alert_email=reader.text("alertEmail"),
            budget_email=reader.text("budgetEmail"),
            budget_usd=reader.number("budgetUsd", DEFAULT_BUDGET_USD),
            allowed_origins=reader.items("allowedOrigins", DEFAULT_ALLOWED_ORIGINS),
            api_rate_limit=reader.integer("apiRateLimit", DEFAULT_API_RATE_LIMIT),
            api_burst_limit=reader.integer("apiBurstLimit", DEFAULT_API_BURST_LIMIT),
            classifier_model=reader.text("classifierModel") or DEFAULT_CLASSIFIER_MODEL,
            judge_model=reader.text("judgeModel") or DEFAULT_JUDGE_MODEL,
            guardrail_id=reader.text("bedrockGuardrailId"),
            guardrail_version=reader.text("bedrockGuardrailVersion") or "DRAFT",
            lambda_asset_path=reader.path("lambdaAssetPath", DEFAULT_LAMBDA_ASSET),
        )


class _ContextReader:
    """Converte o context (texto vindo do `-c` ou tipado vindo do cdk.json) em tipos Python."""

    def __init__(self, node: Node) -> None:
        self._node = node

    def _raw(self, key: str) -> object:
        return self._node.try_get_context(key)

    def text(self, key: str) -> str | None:
        value = self._raw(key)
        if value is None:
            return None
        cleaned = str(value).strip()
        return cleaned or None

    def boolean(self, key: str) -> bool:
        value = self._raw(key)
        if isinstance(value, bool):
            return value
        normalized = str(value or "").strip().lower()
        _check(normalized in _TRUE | _FALSE, f"{key} deve ser true/false, veio {value!r}")
        return normalized in _TRUE

    def number(self, key: str, default: float) -> float:
        raw = self.text(key)
        try:
            return default if raw is None else float(raw)
        except ValueError as exc:
            raise ConfigError(f"{key} deve ser numérico, veio {raw!r}") from exc

    def integer(self, key: str, default: int) -> int:
        raw = self.text(key)
        try:
            return default if raw is None else int(raw)
        except ValueError as exc:
            raise ConfigError(f"{key} deve ser inteiro, veio {raw!r}") from exc

    def items(self, key: str, default: tuple[str, ...]) -> tuple[str, ...]:
        value = self._raw(key)
        if value is None:
            return default
        if isinstance(value, str) and value.strip().startswith("["):
            try:
                value = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ConfigError(f"{key} não é uma lista JSON válida") from exc
        parts = value if isinstance(value, list) else str(value).split(",")
        cleaned = tuple(str(part).strip() for part in parts if str(part).strip())
        return cleaned or default

    def path(self, key: str, default: Path) -> Path:
        raw = self.text(key)
        return default if raw is None else Path(raw).expanduser().resolve()


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise ConfigError(message)


def _check_email(key: str, value: str | None) -> None:
    if value is not None:
        _check(bool(_EMAIL.match(value)), f"{key} não parece um e-mail: {value!r}")


def _check_origins(origins: tuple[str, ...]) -> None:
    _check(bool(origins), "allowedOrigins não pode ser vazio")
    for origin in origins:
        # "*" liberaria qualquer site a chamar a API com o navegador da vítima.
        _check("*" not in origin, "allowedOrigins não aceita curinga '*'")
        _check(
            bool(_ORIGIN.match(origin)),
            f"origem CORS inválida {origin!r}: use https://dominio ou http://localhost",
        )

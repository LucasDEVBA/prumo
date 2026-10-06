"""Leitura de segredos no AWS Secrets Manager (ex.: o token administrativo da API).

O valor do segredo nunca vai para log nem para mensagem de erro: as falhas viram `ProviderError`
só com o código do erro da AWS.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final, Protocol

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from prumo.adapters.aws._common import client_config, provider_error
from prumo.domain.errors import ProviderError

if TYPE_CHECKING:
    from prumo.config import Settings

logger = logging.getLogger(__name__)

SERVICE: Final = "secretsmanager"
OPERATION = "GetSecretValue"


class SecretsClient(Protocol):
    """O pedaço do cliente do Secrets Manager que o Prumo usa (sem stubs tipados instalados)."""

    def get_secret_value(self, *, SecretId: str) -> Mapping[str, Any]: ...  # noqa: N803


def secrets_client(settings: Settings) -> SecretsClient:
    client: SecretsClient = boto3.client(
        SERVICE, region_name=settings.aws_region, config=client_config()
    )
    return client


def read_secret(secret_arn: str, settings: Settings, *, client: SecretsClient | None = None) -> str:
    """Devolve o texto do segredo. Falha cedo (ProviderError) se não der para lê-lo."""
    reader = client if client is not None else secrets_client(settings)
    try:
        response = reader.get_secret_value(SecretId=secret_arn)
    except (ClientError, BotoCoreError) as exc:
        raise provider_error(exc, service=SERVICE, operation=OPERATION) from exc
    # Espaço nas pontas nunca faz parte do token (e aparece fácil ao gravar via arquivo).
    value = response.get("SecretString")
    secret = value.strip() if isinstance(value, str) else ""
    if not secret:
        logger.error("secret_empty_or_binary", extra={"service": SERVICE, "operation": OPERATION})
        raise ProviderError(
            "o segredo não tem texto", details={"service": SERVICE, "operation": OPERATION}
        )
    logger.info("secret_loaded", extra={"service": SERVICE, "operation": OPERATION})
    return secret

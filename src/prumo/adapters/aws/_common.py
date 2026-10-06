"""Peças compartilhadas pelos adaptadores da AWS: configuração do cliente e tradução de erros."""

from __future__ import annotations

import logging
from typing import Final, Literal

from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from prumo.domain.errors import ProviderError

logger = logging.getLogger(__name__)

type RetryMode = Literal["standard", "adaptive"]

CONNECT_TIMEOUT_SECONDS = 3
DEFAULT_READ_TIMEOUT_SECONDS = 5
TOTAL_MAX_ATTEMPTS = 3
"""Contando a primeira tentativa (o `max_attempts` do botocore conta só as repetições)."""
RETRY_MODE: Final = "adaptive"
"""O modo adaptativo soma ao backoff exponencial um limitador do lado do cliente, que segura a
taxa de chamadas quando a AWS começa a devolver throttling (em vez de insistir e piorar)."""


def client_config(
    read_timeout: float = DEFAULT_READ_TIMEOUT_SECONDS,
    *,
    total_max_attempts: int = TOTAL_MAX_ATTEMPTS,
    retry_mode: RetryMode = RETRY_MODE,
) -> Config:
    """Timeouts curtos e retries com backoff para todo cliente boto3 do Prumo."""
    return Config(
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
        read_timeout=read_timeout,
        retries={"total_max_attempts": total_max_attempts, "mode": retry_mode},
    )


def error_code(exc: ClientError | BotoCoreError) -> str:
    """Código estável do erro: o `Error.Code` da AWS ou o nome da exceção do botocore."""
    if isinstance(exc, ClientError):
        return exc.response.get("Error", {}).get("Code", "Unknown")
    return type(exc).__name__


def provider_error(
    exc: ClientError | BotoCoreError, *, service: str, operation: str
) -> ProviderError:
    """Registra a falha e a converte em `ProviderError`, sem vazar a mensagem crua da AWS."""
    code = error_code(exc)
    logger.error(
        "aws_call_failed",
        extra={"service": service, "operation": operation, "error_code": code},
    )
    return ProviderError(
        f"falha ao chamar {service}:{operation}",
        details={"service": service, "operation": operation, "error_code": code},
    )

"""Erros do domínio. Operacionais (esperados) têm código estável para a API."""

from __future__ import annotations

from typing import Any


class PrumoError(Exception):
    """Base de todos os erros esperados do Prumo."""

    code = "prumo_error"
    http_status = 500

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class InvalidInputError(PrumoError):
    code = "invalid_input"
    http_status = 422


class NotFoundError(PrumoError):
    code = "not_found"
    http_status = 404


class ConflictError(PrumoError):
    code = "conflict"
    http_status = 409


class AuthorizationError(PrumoError):
    code = "unauthorized"
    http_status = 401


class ProviderError(PrumoError):
    """Falha em serviço externo (Bedrock, DynamoDB, AppConfig...)."""

    code = "provider_error"
    http_status = 502

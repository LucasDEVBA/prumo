"""Peças comuns aos handlers Lambda."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from prumo.observability import correlation_id

type LambdaEvent = Mapping[str, Any]
"""Evento JSON da Lambda. `Any` porque a forma depende da origem (EventBridge, Step Functions)."""

type LambdaResult = dict[str, Any]
"""Resposta JSON da Lambda (vai para os logs de execução ou para o Step Functions)."""

NO_REQUEST_ID = "-"


def bind_invocation(context: object) -> str:
    """Usa o request id da invocação como correlation id de todos os logs dela."""
    request_id = getattr(context, "aws_request_id", None)
    value = request_id if isinstance(request_id, str) and request_id else NO_REQUEST_ID
    correlation_id.set(value)
    return value

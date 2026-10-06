"""Middlewares: correlation id, cabeçalhos de segurança e limite de requisições."""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from prumo.domain.ids import uuid7
from prumo.observability import correlation_id

_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_MUTATING = {"POST", "PUT", "PATCH", "DELETE"}

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' https://fonts.googleapis.com; "
    "font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)

# A documentação interativa do FastAPI (Swagger UI e ReDoc) carrega scripts e estilos do
# cdn.jsdelivr.net e inicializa com um <script> inline. Com a CSP do painel ela abre em branco.
# A política mais aberta vale SÓ nessas páginas, que não exibem dado de usuário.
DOCS_PATHS = ("/docs", "/redoc")
DOCS_CSP = (
    "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; "
    "font-src https://fonts.gstatic.com; img-src 'self' data: https://fastapi.tiangolo.com "
    "https://cdn.redoc.ly; worker-src blob:; connect-src 'self'; frame-ancestors 'none'; "
    "base-uri 'none'"
)

Handler = Callable[[Request], Awaitable[Response]]


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Handler) -> Response:
        incoming = request.headers.get("x-correlation-id", "")
        value = incoming if _SAFE_ID.match(incoming) else uuid7()
        token = correlation_id.set(value)
        try:
            response = await call_next(request)
        finally:
            correlation_id.reset(token)
        response.headers["X-Correlation-ID"] = value
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Handler) -> Response:
        response = await call_next(request)
        is_docs = request.url.path.startswith(DOCS_PATHS)
        response.headers.setdefault("Content-Security-Policy", DOCS_CSP if is_docs else CSP)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        return response


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Token bucket por IP nas rotas que mudam estado.

    Na AWS, o API Gateway já limita no stage; isto protege a execução local e é a segunda
    camada se a API for exposta de outro jeito.
    """

    def __init__(self, app: object, *, per_minute: int = 120) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._capacity = float(per_minute)
        self._refill = per_minute / 60.0
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    async def dispatch(self, request: Request, call_next: Handler) -> Response:
        if request.method in _MUTATING and not self._allow(_client_ip(request)):
            return JSONResponse(
                status_code=429,
                content={
                    "code": "rate_limited",
                    "message": "Muitas requisições. Tente de novo em alguns segundos.",
                    "details": {},
                    "correlation_id": correlation_id.get(),
                },
                headers={"Retry-After": "5"},
            )
        return await call_next(request)

    def _allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            tokens, last = self._buckets.get(key, (self._capacity, now))
            tokens = min(self._capacity, tokens + (now - last) * self._refill)
            allowed = tokens >= 1
            self._buckets[key] = (tokens - 1 if allowed else tokens, now)
            return allowed


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "desconhecido"

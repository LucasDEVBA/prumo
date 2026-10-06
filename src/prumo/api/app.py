"""API HTTP do Prumo (FastAPI) e o painel."""

from __future__ import annotations

import logging
import secrets
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Header, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from prumo import __version__
from prumo.api.middleware import (
    CorrelationIdMiddleware,
    RateLimitMiddleware,
    SecurityHeadersMiddleware,
)
from prumo.api.schemas import (
    AdvanceIn,
    DecisionIn,
    DecisionOut,
    ErrorOut,
    EventsOut,
    PromptOut,
    PromptsOut,
    QualityOut,
    ReviewIn,
    SimulationOut,
    SimulationSettingsIn,
)
from prumo.config import Settings
from prumo.container import Container, build_container
from prumo.data.leads import RUBRIC_TEXT, expected_class_for
from prumo.domain.errors import AuthorizationError, ConflictError, PrumoError
from prumo.observability import configure_logging, correlation_id
from prumo.simulation.engine import Simulator

logger = logging.getLogger(__name__)

_HERE = Path(__file__).parent
HISTORY_LIMIT = 48


class AppState:
    """Estado compartilhado entre as requisições. O lock serializa o que muda o mundo."""

    def __init__(self, container: Container) -> None:
        self.lock = threading.Lock()
        self.container = container
        self.simulator: Simulator | None = None
        self._boot(container)

    def reset(self) -> None:
        self._boot(build_container(self.container.settings))

    def _boot(self, container: Container) -> None:
        self.container = container
        self.simulator = None
        if container.is_simulated:
            self.simulator = Simulator(container, labels_per_hour=40)
            self.simulator.advance(container.settings.simulation_warmup_hours, quiet=True)
            container.quality.note("Prompt v1 em produção. O Prumo mede a qualidade a cada hora.")


def create_app(container: Container | None = None) -> FastAPI:
    if container is None:
        settings = Settings()
        # Antes de montar o container: a leitura do segredo no cold start já sai em JSON.
        configure_logging(settings.log_level)
        container = build_container(settings)
    else:
        configure_logging(container.settings.log_level)
    built = container

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.prumo = AppState(built)
        logger.info("app_started", extra={"provider": built.settings.provider.value})
        yield

    app = FastAPI(
        title="Prumo",
        version=__version__,
        summary="A taxa real de acerto de uma IA em produção.",
        lifespan=lifespan,
        responses={422: {"model": ErrorOut}, 500: {"model": ErrorOut}},
    )
    app.add_middleware(RateLimitMiddleware, per_minute=240)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(CorrelationIdMiddleware)
    app.mount("/static", StaticFiles(directory=_HERE / "static"), name="static")
    _register_error_handlers(app)
    _register_routes(app, Jinja2Templates(directory=_HERE / "templates"))
    return app


def _state(request: Request) -> AppState:
    state: AppState = request.app.state.prumo
    return state


StateDep = Annotated[AppState, Depends(_state)]


def require_admin(state: StateDep, x_prumo_token: Annotated[str | None, Header()] = None) -> None:
    """Rotas que mudam o sistema ou mostram leads exigem o token.

    No simulador local sem token configurado, tudo fica aberto para o demo. Fora dele, a regra
    é fail-closed: sem token configurado, ninguém passa (a fila mostra dados pessoais e um
    rótulo falso manipularia a estimativa).
    """
    expected = state.container.admin_token
    if expected is None:
        if state.container.is_simulated:
            return
        raise AuthorizationError("token de administrador não configurado no servidor")
    if x_prumo_token is None or not secrets.compare_digest(
        x_prumo_token.encode(), expected.get_secret_value().encode()
    ):
        raise AuthorizationError("token de administrador ausente ou inválido")


Admin = Depends(require_admin)


def _simulator(state: AppState) -> Simulator:
    if state.simulator is None:
        raise ConflictError("o simulador só existe com PRUMO_PROVIDER=simulated")
    return state.simulator


def _register_routes(app: FastAPI, templates: Jinja2Templates) -> None:
    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def dashboard(request: Request, state: StateDep) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "simulated": state.container.is_simulated,
                "rubric": RUBRIC_TEXT.splitlines()[1:],
                "slo": state.container.quality.policy.slo.target,
                "needs_token": state.container.admin_token is not None
                or not state.container.is_simulated,
            },
        )

    @app.get("/health", tags=["sistema"])
    def health(state: StateDep) -> dict[str, str]:
        return {
            "status": "ok",
            "version": __version__,
            "provider": state.container.settings.provider.value,
        }

    @app.get("/api/v1/quality", response_model=QualityOut, tags=["qualidade"])
    def quality(
        state: StateDep, limit: Annotated[int, Query(ge=1, le=500)] = HISTORY_LIMIT
    ) -> QualityOut:
        return QualityOut(
            current=state.container.quality.read(),
            history=state.container.snapshots.recent(limit),
        )

    @app.get("/api/v1/prompts", response_model=PromptsOut, tags=["prompts"])
    def prompts(state: StateDep) -> PromptsOut:
        store = state.container.prompts
        previous = store.previous()
        return PromptsOut(
            active=PromptOut.of(store.active()),
            previous=PromptOut.of(previous) if previous else None,
            catalog=[PromptOut.of(p) for p in store.catalog()],
        )

    @app.post(
        "/api/v1/prompts/{version_id}/deploy",
        response_model=PromptOut,
        dependencies=[Admin],
        tags=["prompts"],
    )
    def deploy(version_id: str, state: StateDep) -> PromptOut:
        with state.lock:
            return PromptOut.of(state.container.quality.deploy(version_id))

    @app.post(
        "/api/v1/prompts/rollback",
        response_model=PromptOut,
        dependencies=[Admin],
        tags=["prompts"],
    )
    def rollback(state: StateDep) -> PromptOut:
        with state.lock:
            return PromptOut.of(state.container.quality.rollback(reason="manual"))

    @app.post(
        "/api/v1/decisions",
        response_model=DecisionOut,
        status_code=status.HTTP_201_CREATED,
        dependencies=[Admin],
        tags=["decisões"],
    )
    def create_decision(body: DecisionIn, state: StateDep) -> DecisionOut:
        with state.lock:
            return DecisionOut.of(state.container.decisions.process(body.lead_text))

    @app.get(
        "/api/v1/review/queue",
        response_model=list[DecisionOut],
        dependencies=[Admin],
        tags=["revisão"],
    )
    def review_queue(
        state: StateDep, limit: Annotated[int, Query(ge=1, le=50)] = 5
    ) -> list[DecisionOut]:
        return [DecisionOut.of(d) for d in state.container.reviews.pending(limit)]

    @app.post(
        "/api/v1/review/{decision_id}",
        response_model=DecisionOut,
        dependencies=[Admin],
        tags=["revisão"],
    )
    def submit_review(decision_id: str, body: ReviewIn, state: StateDep) -> DecisionOut:
        with state.lock:
            labeled = state.container.reviews.submit(
                decision_id, correct=body.correct, reviewer=body.reviewer
            )
        expected = expected_class_for(labeled.lead_text) if state.container.is_simulated else None
        return DecisionOut.of(labeled, expected)

    @app.get("/api/v1/events", response_model=EventsOut, tags=["auditoria"])
    def events(state: StateDep, limit: Annotated[int, Query(ge=1, le=200)] = 30) -> EventsOut:
        return EventsOut(items=state.container.events.recent(limit))

    _register_simulation_routes(app)


def _register_simulation_routes(app: FastAPI) -> None:
    @app.get("/api/v1/simulation", response_model=SimulationOut, tags=["simulador"])
    def simulation(state: StateDep) -> SimulationOut:
        sim = state.simulator
        auto = state.container.quality.policy.auto_rollback
        if sim is None:
            return SimulationOut(enabled=False, auto_rollback=auto)
        return SimulationOut(
            enabled=True,
            clock=state.container.clock.now(),
            labels_per_hour=sim.labels_per_hour,
            leads_per_hour=sim.leads_per_hour,
            auto_rollback=auto,
        )

    @app.post(
        "/api/v1/simulation/advance",
        response_model=QualityOut,
        dependencies=[Admin],
        tags=["simulador"],
    )
    def advance(body: AdvanceIn, state: StateDep) -> QualityOut:
        with state.lock:
            _simulator(state).advance(body.hours)
            return QualityOut(
                current=state.container.quality.read(),
                history=state.container.snapshots.recent(HISTORY_LIMIT),
            )

    @app.put(
        "/api/v1/simulation/settings",
        response_model=SimulationOut,
        dependencies=[Admin],
        tags=["simulador"],
    )
    def update_settings(body: SimulationSettingsIn, state: StateDep) -> SimulationOut:
        with state.lock:
            sim = _simulator(state)
            if body.labels_per_hour is not None:
                sim.set_labels_per_hour(body.labels_per_hour)
            if body.auto_rollback is not None:
                quality = state.container.quality
                quality.policy = replace(quality.policy, auto_rollback=body.auto_rollback)
                quality.note(
                    "Rollback automático ligado."
                    if body.auto_rollback
                    else "Rollback automático desligado: o alarme só avisa."
                )
        return simulation(state)

    @app.post(
        "/api/v1/simulation/reset",
        response_model=SimulationOut,
        dependencies=[Admin],
        tags=["simulador"],
    )
    def reset(state: StateDep) -> SimulationOut:
        with state.lock:
            _simulator(state)
            state.reset()
        return simulation(state)


def _register_error_handlers(app: FastAPI) -> None:
    def _body(code: str, message: str, details: dict[str, object]) -> dict[str, object]:
        return ErrorOut(
            code=code, message=message, details=details, correlation_id=correlation_id.get()
        ).model_dump()

    @app.exception_handler(PrumoError)
    async def prumo_error(request: Request, exc: PrumoError) -> JSONResponse:
        if exc.http_status >= 500:
            logger.error("operational_error", extra={"code": exc.code, "path": request.url.path})
        return JSONResponse(
            status_code=exc.http_status, content=_body(exc.code, exc.message, exc.details)
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {"field": ".".join(str(p) for p in e["loc"][1:]), "message": e["msg"]}
            for e in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content=_body("invalid_input", "Dados inválidos.", {"errors": errors}),
        )

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception) -> JSONResponse:
        # Erro de programação: registra com stack trace, mas nunca a expõe ao cliente.
        logger.exception("unexpected_error", extra={"path": request.url.path})
        return JSONResponse(
            status_code=500,
            content=_body(
                "internal_error", "Erro interno. Use o correlation_id para rastrear.", {}
            ),
        )

"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from conductor import __version__
from conductor.api.errors import ErrorResponse, install_error_handlers
from conductor.api.hub import EventHub
from conductor.api.request_context import RequestContextMiddleware
from conductor.api.routes import demo, health, metrics, resources, stream
from conductor.core.config import Settings, get_settings
from conductor.core.db import asyncpg_dsn, create_engine, create_session_factory
from conductor.tasks import builtin_registry

API_PREFIX = "/api/v1"

DESCRIPTION = """
Conductor is a distributed job scheduling and workflow engine that uses
PostgreSQL as its only data store.

* **Delivery** is at-least-once; side-effecting tasks are effectively-once via
  idempotency keys. See `docs/semantics.md` for the exact guarantees.
* **Errors** always have the shape `{"error": {"code", "message", "request_id", "details"}}`.
* **Live updates** stream from `GET /api/v1/stream` (Server-Sent Events).
"""


def create_app(settings: Settings | None = None, *, start_hub: bool = True) -> FastAPI:
    """Build the API app.

    The engine is created eagerly (it does not connect until first use) so the
    app is usable even when a test client skips lifespan events. The event
    hub starts in the lifespan, so in-process tests without lifespan simply
    have no live stream.
    """
    settings = settings or get_settings()
    engine = create_engine(settings)
    hub = EventHub(engine=engine, dsn=asyncpg_dsn(settings.database_url))

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if start_hub:
            await hub.start()
        try:
            yield
        finally:
            await hub.stop()
            await engine.dispose()

    app = FastAPI(
        title="Conductor API",
        version=__version__,
        description=DESCRIPTION,
        lifespan=lifespan,
        responses={500: {"model": ErrorResponse, "description": "Unexpected server error."}},
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)
    app.state.registry = builtin_registry()
    app.state.hub = hub

    install_error_handlers(app)
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
            allow_methods=["GET", "POST", "PUT"],
            allow_headers=["Content-Type", "Last-Event-ID", "X-Request-ID", "X-Sandbox-Id"],
            expose_headers=["X-Request-ID"],
            max_age=600,
        )
    app.add_middleware(RequestContextMiddleware)

    v1 = APIRouter(prefix=API_PREFIX)
    v1.include_router(health.router)
    v1.include_router(resources.router)
    v1.include_router(stream.router)
    v1.include_router(metrics.router)
    v1.include_router(demo.router)
    app.include_router(v1)
    # Prometheus convention: also serve metrics at the root path.
    app.add_api_route("/metrics", metrics.metrics, include_in_schema=False)
    return app

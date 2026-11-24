"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI

from conductor import __version__
from conductor.api.errors import ErrorResponse, install_error_handlers
from conductor.api.request_context import RequestContextMiddleware
from conductor.api.routes import health
from conductor.core.config import Settings, get_settings
from conductor.core.db import create_engine, create_session_factory

API_PREFIX = "/api/v1"


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the API app.

    The engine is created eagerly (it does not connect until first use) so the
    app is fully usable even when a test client skips lifespan events; the
    lifespan only disposes of the pool on shutdown.
    """
    settings = settings or get_settings()
    engine = create_engine(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await engine.dispose()

    app = FastAPI(
        title="Conductor API",
        version=__version__,
        description="Distributed job scheduling and workflow engine.",
        lifespan=lifespan,
        responses={500: {"model": ErrorResponse, "description": "Unexpected server error."}},
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)

    install_error_handlers(app)
    app.add_middleware(RequestContextMiddleware)

    v1 = APIRouter(prefix=API_PREFIX)
    v1.include_router(health.router)
    app.include_router(v1)
    return app

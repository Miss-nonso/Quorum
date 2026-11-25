"""Liveness and readiness probe."""

from __future__ import annotations

import asyncio
import logging
from typing import Literal

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel

from quorum import __version__
from quorum.core.db import ping

router = APIRouter(tags=["health"])
logger = logging.getLogger(__name__)

# A probe that hangs is worse than one that fails: keep it well under typical
# load-balancer and Docker healthcheck timeouts.
_DB_CHECK_TIMEOUT_S = 2.0


class HealthChecks(BaseModel):
    """Status of each dependency the process needs to serve traffic."""

    database: Literal["ok", "unreachable"]


class HealthResponse(BaseModel):
    """Probe result. ``status`` is ``ok`` only if every check passed."""

    status: Literal["ok", "unavailable"]
    version: str
    checks: HealthChecks


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Health check",
    responses={503: {"model": HealthResponse, "description": "A dependency is unavailable."}},
)
async def health(request: Request, response: Response) -> HealthResponse:
    """Report whether the API can reach PostgreSQL.

    Returns 200 when healthy and 503 otherwise, with the same body shape in
    both cases so probes and humans can see which check failed.
    """
    try:
        async with asyncio.timeout(_DB_CHECK_TIMEOUT_S):
            await ping(request.app.state.engine)
        database: Literal["ok", "unreachable"] = "ok"
    except Exception as exc:  # any failure means "not ready"; the reason goes to logs
        logger.warning("health check: database unreachable", extra={"error": repr(exc)})
        database = "unreachable"

    healthy = database == "ok"
    if not healthy:
        response.status_code = 503
    return HealthResponse(
        status="ok" if healthy else "unavailable",
        version=__version__,
        checks=HealthChecks(database=database),
    )

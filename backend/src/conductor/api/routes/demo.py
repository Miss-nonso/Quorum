"""Public demo endpoints: dataset, sandbox state, demo runs and chaos controls.

All writes here are scoped to the caller's sandbox (``X-Sandbox-Id``),
rate-limited per client IP, and capped globally in the database. Chaos only
ever touches workers started with ``CONDUCTOR_DEMO_CHAOS=true``.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Request, status
from pydantic import BaseModel

from conductor.api.deps import ReadConn, WriteConn
from conductor.api.errors import ApiError, ErrorResponse
from conductor.api.schemas import RunOut
from conductor.core.config import Settings
from conductor.demo import service
from conductor.demo.limits import SlidingWindowLimiter
from conductor.samples import DEMO_DATASET
from conductor.store import queries

router = APIRouter(prefix="/demo", tags=["demo"])
LIMITED: dict[int | str, dict[str, Any]] = {
    403: {"model": ErrorResponse, "description": "Demo mode is off."},
    429: {"model": ErrorResponse, "description": "Rate or capacity limit; see Retry-After."},
}


@router.get("/dataset", summary="The sample workflow's input data")
async def dataset() -> list[dict[str, Any]]:
    """A small, fixed list of orders that the sample workflow's ``fetch`` step downloads.

    Served by the API itself so the demo needs no third-party service.
    """
    return DEMO_DATASET


# ------------------------------------------------------------------ guards


class Limiters:
    def __init__(self, settings: Settings) -> None:
        self.actions = SlidingWindowLimiter(settings.demo_actions_per_ip_per_min, 60)
        self.kills_global = SlidingWindowLimiter(settings.demo_kills_per_min, 60)
        self.kills_per_ip = SlidingWindowLimiter(1, 10)


def _settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def _limiters(request: Request) -> Limiters:
    if not hasattr(request.app.state, "demo_limiters"):
        request.app.state.demo_limiters = Limiters(_settings(request))
    limiters: Limiters = request.app.state.demo_limiters
    return limiters


def _client_ip(request: Request) -> str:
    # uvicorn resolves X-Forwarded-For from trusted proxies (Caddy) into client.host.
    return request.client.host if request.client else "unknown"


def _too_many(message: str, retry_after: float) -> ApiError:
    error = ApiError(429, "rate_limited", message, {"retry_after_s": round(retry_after, 1)})
    error.headers = {"Retry-After": str(max(1, round(retry_after)))}
    return error


async def demo_sandbox(
    request: Request,
    x_sandbox_id: Annotated[str, Header(description="Random per-visitor id, made by the browser.")],
) -> str:
    """Demo writes need demo mode, a well-formed sandbox id and an unspent rate budget."""
    if not _settings(request).demo_mode:
        raise ApiError(403, "demo_disabled", "Demo endpoints are disabled on this deployment.")
    if not service.SANDBOX_ID.match(x_sandbox_id):
        raise ApiError(422, "invalid_sandbox", "X-Sandbox-Id must be 16-64 URL-safe characters.")
    wait = _limiters(request).actions.check(_client_ip(request))
    if wait:
        raise _too_many("Slow down a little: too many demo actions from your address.", wait)
    return x_sandbox_id


Sandbox = Annotated[str, Depends(demo_sandbox)]


def _limit_error(exc: Exception) -> ApiError:
    if isinstance(exc, service.DemoLimitError):
        return _too_many(str(exc), 5)
    return ApiError(409, "demo_unavailable", str(exc))


# ------------------------------------------------------------------ endpoints


class SandboxOut(BaseModel):
    sandbox_id: str
    failures_enabled: bool
    runs: list[dict[str, Any]]
    flood: dict[str, Any]


@router.get("/sandbox", response_model=SandboxOut, summary="Your sandbox", responses=LIMITED)
async def sandbox(sandbox_id: Sandbox, conn: ReadConn) -> SandboxOut:
    return SandboxOut.model_validate(await service.sandbox_state(conn, sandbox_id))


@router.post(
    "/runs",
    status_code=status.HTTP_201_CREATED,
    response_model=RunOut,
    summary="Run the sample pipeline in your sandbox",
    responses=LIMITED,
)
async def start_run(
    sandbox_id: Sandbox, conn: WriteConn, settings: Annotated[Settings, Depends(_settings)]
) -> RunOut:
    try:
        created = await service.start_demo_run(conn, sandbox_id, settings)
    except (service.DemoLimitError, service.DemoUnavailableError) as exc:
        raise _limit_error(exc) from exc
    return RunOut.model_validate(await queries.get_run(conn, created.run_id))


class KillOut(BaseModel):
    worker_id: str
    hostname: str
    tasks: list[str]


@router.post(
    "/chaos/kill-worker",
    response_model=KillOut,
    summary="Crash a demo worker (preferably one running your tasks)",
    responses=LIMITED,
)
async def kill_worker(request: Request, sandbox_id: Sandbox, conn: WriteConn) -> KillOut:
    """The worker crashes on its next heartbeat (``os._exit``), mid-task, without cleanup.

    Its leases expire, the reaper requeues its tasks, and another worker
    finishes them. The container restarts it as a brand-new worker.
    """
    limiters = _limiters(request)
    wait = limiters.kills_per_ip.check(_client_ip(request)) or limiters.kills_global.check("all")
    if wait:
        raise _too_many("Workers need a moment to recover before the next kill.", wait)
    try:
        result = await service.kill_worker(conn, sandbox_id)
    except service.DemoUnavailableError as exc:
        raise _limit_error(exc) from exc
    return KillOut(worker_id=str(result.worker_id), hostname=result.hostname, tasks=result.tasks)


class FloodOut(BaseModel):
    low: int
    high: int


@router.post(
    "/chaos/flood",
    response_model=FloodOut,
    summary="Flood the queue with low- and high-priority jobs",
    responses=LIMITED,
)
async def flood(
    sandbox_id: Sandbox, conn: WriteConn, settings: Annotated[Settings, Depends(_settings)]
) -> FloodOut:
    try:
        result = await service.flood(conn, sandbox_id, settings)
    except service.DemoLimitError as exc:
        raise _limit_error(exc) from exc
    return FloodOut(low=result.low, high=result.high)


class FailuresIn(BaseModel):
    enabled: bool


@router.post(
    "/chaos/toggle-failures",
    response_model=FailuresIn,
    summary="Inject failures into your next demo runs",
    responses=LIMITED,
)
async def toggle_failures(body: FailuresIn, sandbox_id: Sandbox, conn: WriteConn) -> FailuresIn:
    """When on, new runs retry `transform` twice and dead-letter `notify` until you replay it."""
    await service.set_failures(conn, sandbox_id, body.enabled)
    return body

"""All-in-one mode: the whole backend in one container, for free hosting tiers.

Free platforms (e.g. Render's free web service) run a single web process and
offer no free background workers. ``quorum all`` fits the system into one:

* the **API** (uvicorn) serves on ``$PORT``;
* the **scheduler** (leader election, reaper, cron, aging, demo reset) runs as
  a task inside the same event loop;
* **workers** run as real subprocesses under a small supervisor, which
  respawns any that exit. A demo "kill" therefore still crashes a genuine
  process mid-task, its lease still expires, another worker still finishes
  the task, and a fresh worker (new id) replaces it, exactly as Docker's
  restart policy does in the multi-container deployment.

Nothing about the engine changes: processes still coordinate only through
PostgreSQL.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from quorum.api.app import create_app
from quorum.core.config import Settings
from quorum.scheduler.service import Scheduler, SchedulerConfig

logger = logging.getLogger(__name__)


class WorkerSupervisor:
    """Keep ``count`` worker subprocesses running; respawn any that exit."""

    def __init__(self, count: int, *, restart_delay_s: float = 1.0) -> None:
        self.count = count
        self.restart_delay_s = restart_delay_s
        self._procs: dict[int, asyncio.subprocess.Process] = {}
        self._tasks: list[asyncio.Task[None]] = []
        self._stopping = asyncio.Event()
        self.spawned = 0

    async def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._keep_alive(slot), name=f"worker-slot-{slot}")
            for slot in range(self.count)
        ]

    async def _keep_alive(self, slot: int) -> None:
        while not self._stopping.is_set():
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "quorum", "worker", env=os.environ.copy()
            )
            self._procs[slot] = proc
            self.spawned += 1
            logger.info("worker started", extra={"slot": slot, "pid": proc.pid})
            code = await proc.wait()
            if self._stopping.is_set():
                return
            logger.warning(
                "worker exited; restarting", extra={"slot": slot, "pid": proc.pid, "code": code}
            )
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(self.restart_delay_s):
                    await self._stopping.wait()

    async def stop(self, grace_s: float) -> None:
        """SIGTERM every worker (they drain and release), then SIGKILL stragglers."""
        self._stopping.set()
        live = [p for p in self._procs.values() if p.returncode is None]
        for proc in live:
            with contextlib.suppress(ProcessLookupError):
                proc.send_signal(signal.SIGTERM)
        try:
            async with asyncio.timeout(grace_s):
                await asyncio.gather(*(p.wait() for p in live))
        except TimeoutError:
            for proc in live:
                if proc.returncode is None:
                    with contextlib.suppress(ProcessLookupError):
                        proc.kill()
        for keeper in self._tasks:
            keeper.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)


def create_all_in_one_app(settings: Settings, *, workers: int) -> FastAPI:
    """The API app, whose lifespan also runs the scheduler and the worker supervisor."""
    app = create_app(settings)
    inner = app.router.lifespan_context
    supervisor = WorkerSupervisor(workers)
    scheduler = Scheduler(
        engine=app.state.engine,
        database_url=settings.database_url,
        config=SchedulerConfig.from_settings(settings),
    )
    app.state.supervisor = supervisor

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        async with inner(application):
            scheduler_task = asyncio.create_task(scheduler.run(), name="scheduler")
            await supervisor.start()
            logger.info("all-in-one backend started", extra={"workers": workers})
            try:
                yield
            finally:
                await supervisor.stop(settings.shutdown_grace_s)
                scheduler.request_stop()
                with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                    await asyncio.wait_for(scheduler_task, timeout=5)

    app.router.lifespan_context = lifespan
    return app

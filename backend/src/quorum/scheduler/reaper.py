"""The reaper: recovers tasks from workers that died or stalled.

It is deliberately safe to run in more than one process at once (rows are
claimed with SKIP LOCKED and conditions re-checked under lock), so failure
recovery does not depend on leader election working. Phase 3's leader
election only avoids the duplicated polling effort.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine

from quorum.core.config import Settings
from quorum.store import tasks as task_store
from quorum.store import workers as worker_store

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ReaperConfig:
    interval_s: float = 5.0
    batch_size: int = 100
    worker_unhealthy_after_s: float = 15.0
    worker_dead_after_s: float = 60.0

    @classmethod
    def from_settings(cls, settings: Settings) -> ReaperConfig:
        return cls(
            interval_s=settings.reaper_interval_s,
            batch_size=settings.reaper_batch_size,
            worker_unhealthy_after_s=settings.worker_unhealthy_after_s,
            worker_dead_after_s=settings.worker_dead_after_s,
        )


class Reaper:
    """Periodically requeue or dead-letter expired leases and flag silent workers."""

    def __init__(self, *, engine: AsyncEngine, config: ReaperConfig) -> None:
        self._engine = engine
        self._config = config
        self._stopping = asyncio.Event()
        self._rng = random.Random()

    def request_stop(self) -> None:
        self._stopping.set()

    async def run_once(self) -> int:
        """One sweep. Returns the number of tasks recovered."""
        recovered = 0
        while True:
            async with self._engine.begin() as conn:
                reaped = await task_store.reap_expired_leases(
                    conn, limit=self._config.batch_size, rng=self._rng
                )
            for item in reaped:
                logger.warning(
                    "lease expired; task recovered",
                    extra={
                        "task_id": str(item.task_id),
                        "previous_owner": str(item.previous_owner),
                        "outcome": item.outcome.status.value,
                        "retry_in_s": item.outcome.delay_s,
                    },
                )
            recovered += len(reaped)
            if len(reaped) < self._config.batch_size:
                break
        async with self._engine.begin() as conn:
            changes = await worker_store.mark_stale_workers(
                conn,
                unhealthy_after_s=self._config.worker_unhealthy_after_s,
                dead_after_s=self._config.worker_dead_after_s,
            )
        for change in changes:
            logger.warning(
                "worker heartbeat lapsed",
                extra={"worker": str(change.worker_id), "status": change.status},
            )
        return recovered

    async def run(self) -> None:
        """Sweep every ``interval_s`` until stopped. Database errors are logged and retried."""
        logger.info("reaper started", extra={"interval_s": self._config.interval_s})
        while not self._stopping.is_set():
            try:
                await self.run_once()
            except Exception:
                logger.exception("reaper sweep failed")
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(self._config.interval_s):
                    await self._stopping.wait()
        logger.info("reaper stopped")

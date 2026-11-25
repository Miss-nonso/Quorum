"""The scheduler process: campaign for leadership, then run the leader's duties.

Duties (only on the leader):

* **reaper**: recover expired leases, flag silent workers;
* **cron**: start runs for due schedules;
* **aging**: raise the effective priority of waiting tasks.

Run advancement needs no loop: it happens inside each task's completion
transaction (see ``store/runs.py``).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncEngine

from quorum.core.config import Settings
from quorum.core.db import asyncpg_dsn
from quorum.demo import service as demo_service
from quorum.scheduler.leader import LeaderElector
from quorum.scheduler.reaper import Reaper, ReaperConfig
from quorum.store import aging as aging_store
from quorum.store import schedules as schedule_store

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SchedulerConfig:
    reaper: ReaperConfig = field(default_factory=ReaperConfig)
    cron_tick_s: float = 1.0
    aging_tick_s: float = 5.0
    aging_interval_s: float = 10.0
    aging_max_bonus: int = 200
    leader_check_interval_s: float = 2.0
    leader_retry_interval_s: float = 2.0
    demo_reset_interval_s: float | None = None  # set only on the public demo
    demo_retention_s: float = 1800.0

    @classmethod
    def from_settings(cls, settings: Settings) -> SchedulerConfig:
        return cls(
            reaper=ReaperConfig.from_settings(settings),
            cron_tick_s=settings.cron_tick_s,
            aging_tick_s=settings.aging_tick_s,
            aging_interval_s=settings.aging_interval_s,
            aging_max_bonus=settings.aging_max_bonus,
            leader_check_interval_s=settings.leader_check_interval_s,
            leader_retry_interval_s=settings.leader_retry_interval_s,
            demo_reset_interval_s=settings.demo_reset_interval_s if settings.demo_mode else None,
            demo_retention_s=settings.demo_retention_s,
        )


async def _every(interval_s: float, name: str, step: Callable[[], Awaitable[object]]) -> None:
    """Run ``step`` forever with ``interval_s`` pauses; log (don't die on) failures."""
    while True:
        try:
            await step()
        except Exception:
            logger.exception("%s step failed", name)
        await asyncio.sleep(interval_s)


class Scheduler:
    def __init__(self, *, engine: AsyncEngine, database_url: str, config: SchedulerConfig) -> None:
        self._engine = engine
        self._config = config
        self.elector = LeaderElector(
            dsn=asyncpg_dsn(database_url),
            check_interval_s=config.leader_check_interval_s,
            retry_interval_s=config.leader_retry_interval_s,
        )
        self.reaper = Reaper(engine=engine, config=config.reaper)

    def request_stop(self) -> None:
        self.elector.request_stop()

    async def run(self) -> None:
        logger.info("scheduler started", extra={"instance_id": str(self.elector.id)})
        await self.elector.run(self._duties)
        logger.info("scheduler stopped")

    async def fire_schedules(self) -> int:
        async with self._engine.begin() as conn:
            firings = await schedule_store.fire_due_schedules(conn)
        for f in firings:
            logger.info(
                "schedule fired",
                extra={
                    "workflow_id": str(f.workflow_id),
                    "scheduled_for": f.scheduled_for.isoformat(),
                    "run_id": str(f.run_id) if f.run_id else None,
                    "missed": f.missed,
                    "next_fire_at": f.next_fire_at.isoformat(),
                },
            )
        return len(firings)

    async def age_priorities(self) -> int:
        async with self._engine.begin() as conn:
            return await aging_store.apply_aging(
                conn,
                interval_s=self._config.aging_interval_s,
                max_bonus=self._config.aging_max_bonus,
            )

    async def reset_demo(self) -> dict[str, int]:
        async with self._engine.begin() as conn:
            deleted = await demo_service.reset_demo_data(
                conn, retention_s=self._config.demo_retention_s
            )
        if any(deleted.values()):
            logger.info("demo data reset", extra={"deleted": deleted})
        return deleted

    async def _duties(self) -> None:
        c = self._config
        async with asyncio.TaskGroup() as group:
            group.create_task(_every(c.reaper.interval_s, "reaper", self.reaper.run_once))
            group.create_task(_every(c.cron_tick_s, "cron", self.fire_schedules))
            group.create_task(_every(c.aging_tick_s, "aging", self.age_priorities))
            if c.demo_reset_interval_s is not None:
                group.create_task(_every(c.demo_reset_interval_s, "demo reset", self.reset_demo))

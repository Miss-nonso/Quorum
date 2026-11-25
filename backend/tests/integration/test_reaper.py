"""The reaper loop, in-process."""

from __future__ import annotations

import asyncio
import uuid

import asyncpg
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from quorum.scheduler.reaper import Reaper, ReaperConfig
from quorum.store import tasks as task_store
from quorum.store import workers as worker_store
from tests.conftest import UNREACHABLE_DB_URL
from tests.integration.helpers import FAST_RETRY, enqueue

WORKER = uuid.UUID(int=0xDEAD)


async def test_one_sweep_drains_more_than_a_batch_and_flags_silent_workers(
    engine: AsyncEngine, db: asyncpg.Connection
) -> None:
    for _ in range(5):
        await enqueue(engine, backoff=FAST_RETRY)
    async with engine.begin() as conn:
        await worker_store.register_worker(
            conn, worker_id=WORKER, hostname="h", pid=1, concurrency=5, capabilities=[]
        )
        await task_store.claim_tasks(conn, worker_id=WORKER, limit=5, lease_ttl_s=30)
    await db.execute(
        "UPDATE task_instances SET lease_expires_at = now() - interval '1 second';"
        "UPDATE workers SET last_heartbeat_at = now() - interval '20 seconds'"
    )

    reaper = Reaper(engine=engine, config=ReaperConfig(batch_size=2, worker_unhealthy_after_s=10))
    assert await reaper.run_once() == 5
    assert await db.fetchval("SELECT count(*) FROM task_instances WHERE status = 'ready'") == 5
    assert await db.fetchval("SELECT status FROM workers WHERE id = $1", WORKER) == "unhealthy"
    assert await reaper.run_once() == 0


async def test_run_loop_sweeps_until_stopped(engine: AsyncEngine, db: asyncpg.Connection) -> None:
    job = await enqueue(engine, backoff=FAST_RETRY)
    async with engine.begin() as conn:
        await task_store.claim_tasks(conn, worker_id=WORKER, limit=1, lease_ttl_s=0.1)

    reaper = Reaper(engine=engine, config=ReaperConfig(interval_s=0.05))
    runner = asyncio.create_task(reaper.run())
    for _ in range(100):
        if (
            await db.fetchval("SELECT status FROM task_instances WHERE id = $1", job.task_id)
            == "ready"
        ):
            break
        await asyncio.sleep(0.05)
    else:
        pytest.fail("reaper loop never recovered the task")
    reaper.request_stop()
    await asyncio.wait_for(runner, timeout=2)


async def test_run_loop_survives_database_outages(caplog: pytest.LogCaptureFixture) -> None:
    broken = create_async_engine(UNREACHABLE_DB_URL)
    reaper = Reaper(engine=broken, config=ReaperConfig(interval_s=0.05))
    runner = asyncio.create_task(reaper.run())
    await asyncio.sleep(0.3)
    reaper.request_stop()
    await asyncio.wait_for(runner, timeout=2)
    await broken.dispose()
    failures = [r for r in caplog.records if r.getMessage() == "reaper sweep failed"]
    assert len(failures) >= 2, "it should keep retrying, not crash on the first error"

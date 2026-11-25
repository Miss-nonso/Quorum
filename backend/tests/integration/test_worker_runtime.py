"""The worker runtime, in-process, against a real PostgreSQL."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace

import asyncpg
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from quorum.domain.backoff import BackoffPolicy
from quorum.store import tasks as task_store
from quorum.worker.runtime import Worker, WorkerConfig
from tests.conftest import eventually
from tests.integration.helpers import FAST_RETRY, REGISTRY, enqueue, event_types, task_row

FAST = WorkerConfig(
    concurrency=4,
    lease_ttl_s=2.0,
    heartbeat_interval_s=0.1,
    poll_interval_s=0.1,
    shutdown_grace_s=5.0,
    listen=False,
)


@asynccontextmanager
async def running_worker(
    engine: AsyncEngine, database_url: str, config: WorkerConfig = FAST
) -> AsyncIterator[Worker]:
    worker = Worker(engine=engine, registry=REGISTRY, config=config, database_url=database_url)
    runner = asyncio.create_task(worker.run())
    try:
        yield worker
    finally:
        worker.request_stop()
        await asyncio.wait_for(runner, timeout=10)


async def status_of(db: asyncpg.Connection, task_id: object) -> str:
    return str(await db.fetchval("SELECT status FROM task_instances WHERE id = $1", task_id))


async def test_worker_runs_tasks_to_success_and_registers_itself(
    engine: AsyncEngine, database_url: str, db: asyncpg.Connection
) -> None:
    jobs = [await enqueue(engine, "sleep", {"seconds": 0.05}) for _ in range(10)]
    async with running_worker(engine, database_url) as worker:

        async def all_done() -> bool:
            done = await db.fetchval(
                "SELECT count(*) FROM task_instances WHERE status = 'succeeded'"
            )
            return bool(done == len(jobs))

        await eventually(all_done, within=10, what="all tasks to succeed")
        row = await db.fetchrow("SELECT * FROM workers WHERE id = $1", worker.id)
        assert row is not None
        assert row["status"] == "active"
        assert row["capabilities"] == REGISTRY.names()
    assert await db.fetchval("SELECT status FROM workers WHERE id = $1", worker.id) == "stopped"
    result = await db.fetchval("SELECT result FROM task_instances WHERE id = $1", jobs[0].task_id)
    assert result == '{"slept_s": 0.05}'


async def test_flaky_task_retries_until_it_succeeds(
    engine: AsyncEngine, database_url: str, db: asyncpg.Connection
) -> None:
    job = await enqueue(
        engine,
        "flaky",
        {"failure_rate": 0, "fail_first_attempts": 2},
        max_attempts=3,
        backoff=FAST_RETRY,
    )
    async with running_worker(engine, database_url):
        await eventually(
            lambda: _is(db, job.task_id, "succeeded"), within=10, what="flaky task to succeed"
        )
    row = await task_row(db, job.task_id)
    assert row["attempt"] == 3
    assert (await event_types(db, job.task_id)).count("task_retry_scheduled") == 2


async def test_task_that_always_fails_is_dead_lettered(
    engine: AsyncEngine, database_url: str, db: asyncpg.Connection
) -> None:
    job = await enqueue(engine, "flaky", {"failure_rate": 1}, max_attempts=2, backoff=FAST_RETRY)
    async with running_worker(engine, database_url):
        await eventually(lambda: _is(db, job.task_id, "dead"), within=10, what="dead letter")
    error = await db.fetchval("SELECT error FROM task_instances WHERE id = $1", job.task_id)
    assert '"FlakyTaskError"' in error
    assert (
        await db.fetchval("SELECT count(*) FROM dead_letters WHERE task_id = $1", job.task_id) == 1
    )


async def test_timeout_counts_as_a_failed_attempt(
    engine: AsyncEngine, database_url: str, db: asyncpg.Connection
) -> None:
    job = await enqueue(engine, "sleep", {"seconds": 30}, timeout_s=0.2, max_attempts=1)
    async with running_worker(engine, database_url):
        await eventually(lambda: _is(db, job.task_id, "dead"), within=10, what="timeout")
    error = await db.fetchval(
        "SELECT error->>'kind' FROM task_instances WHERE id = $1", job.task_id
    )
    assert error == "timeout"


async def test_invalid_params_at_execution_time_fail_permanently(
    engine: AsyncEngine, database_url: str, db: asyncpg.Connection
) -> None:
    """Params are re-validated on execution (defence in depth against bad rows)."""
    job = await enqueue(engine, "sleep", {"seconds": 0}, max_attempts=5)
    await db.execute(
        "UPDATE task_instances SET params = '{\"seconds\": -1}' WHERE id = $1", job.task_id
    )
    async with running_worker(engine, database_url):
        await eventually(lambda: _is(db, job.task_id, "dead"), within=10, what="permanent fail")
    assert (await task_row(db, job.task_id))["attempt"] == 1


async def test_worker_abandons_a_task_whose_lease_was_taken_over(
    engine: AsyncEngine, database_url: str, db: asyncpg.Connection
) -> None:
    job = await enqueue(engine, "sleep", {"seconds": 30})
    async with running_worker(engine, database_url) as worker:
        await eventually(lambda: _is(db, job.task_id, "running"), what="task to start")
        # Another claimer took the task over: its fencing token moved on.
        await db.execute(
            "UPDATE task_instances SET fencing_token = fencing_token + 1 WHERE id = $1",
            job.task_id,
        )

        async def abandoned() -> bool:
            return "lease_lost" in await event_types(db, job.task_id) and not worker._inflight

        await eventually(abandoned, within=5, what="worker to abandon the task")
    assert await status_of(db, job.task_id) == "running", "the new owner's lease is untouched"
    assert "task_succeeded" not in await event_types(db, job.task_id)


async def test_graceful_shutdown_drains_short_tasks(
    engine: AsyncEngine, database_url: str, db: asyncpg.Connection
) -> None:
    job = await enqueue(engine, "sleep", {"seconds": 0.5})
    async with running_worker(engine, database_url):
        await eventually(lambda: _is(db, job.task_id, "running"), what="task to start")
    # Leaving the context requested a stop; the in-flight task finished first.
    assert await status_of(db, job.task_id) == "succeeded"


async def test_graceful_shutdown_releases_tasks_that_outlive_the_grace_period(
    engine: AsyncEngine, database_url: str, db: asyncpg.Connection
) -> None:
    job = await enqueue(engine, "sleep", {"seconds": 30})
    config = replace(FAST, shutdown_grace_s=0.2)
    async with running_worker(engine, database_url, config):
        await eventually(lambda: _is(db, job.task_id, "running"), what="task to start")
    row = await task_row(db, job.task_id)
    assert (row["status"], row["attempt"]) == ("ready", 0)
    assert (await event_types(db, job.task_id))[-1] == "task_released"


async def test_notify_suppresses_duplicate_delivery(
    engine: AsyncEngine, database_url: str, db: asyncpg.Connection
) -> None:
    job = await enqueue(engine, "notify", {"message": "hello"})
    # Simulate an earlier attempt that delivered, then died before completing.
    await db.execute(
        "INSERT INTO notifications (idempotency_key, channel, message) "
        "VALUES ($1, 'general', 'hello')",
        job.idempotency_key,
    )
    async with running_worker(engine, database_url):
        await eventually(lambda: _is(db, job.task_id, "succeeded"), what="notify to finish")
    result = await db.fetchval("SELECT result FROM task_instances WHERE id = $1", job.task_id)
    assert result == '{"delivered": false, "duplicate_suppressed": true}'
    assert await db.fetchval("SELECT count(*) FROM notifications") == 1


async def test_listen_wakes_an_idle_worker_long_before_the_poll_interval(
    engine: AsyncEngine, database_url: str, db: asyncpg.Connection
) -> None:
    config = replace(FAST, poll_interval_s=30.0, listen=True)
    async with running_worker(engine, database_url, config):
        await asyncio.sleep(0.5)  # let the worker go idle and the listener connect
        started = time.monotonic()
        job = await enqueue(engine, "sleep", {"seconds": 0})
        await eventually(lambda: _is(db, job.task_id, "succeeded"), within=5, what="wake-up")
        assert time.monotonic() - started < 5


async def test_retry_backoff_is_honoured_by_the_worker(
    engine: AsyncEngine, database_url: str, db: asyncpg.Connection
) -> None:
    job = await enqueue(
        engine,
        "flaky",
        {"failure_rate": 0, "fail_first_attempts": 1},
        backoff=BackoffPolicy(base_s=0.6, max_s=0.6, jitter="none"),
    )
    async with running_worker(engine, database_url):
        await eventually(lambda: _is(db, job.task_id, "succeeded"), within=10, what="retry")
    gap = await db.fetchval(
        """
        SELECT EXTRACT(EPOCH FROM max(at) FILTER (WHERE type = 'task_claimed')
                                - min(at) FILTER (WHERE type = 'task_retry_scheduled'))
        FROM run_events WHERE task_id = $1
        """,
        job.task_id,
    )
    assert float(gap) >= 0.55


async def _is(db: asyncpg.Connection, task_id: object, status: str) -> bool:
    return await status_of(db, task_id) == status


async def test_worker_stops_all_work_when_it_cannot_renew_for_a_whole_lease(
    engine: AsyncEngine,
    database_url: str,
    db: asyncpg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If heartbeats fail for lease_ttl_s, the worker must assume its leases are gone."""
    job = await enqueue(engine, "sleep", {"seconds": 30})
    config = replace(FAST, lease_ttl_s=0.6, heartbeat_interval_s=0.1)
    async with running_worker(engine, database_url, config) as worker:
        await eventually(lambda: _is(db, job.task_id, "running"), what="task to start")

        async def database_unreachable(*_: object, **__: object) -> set[object]:
            raise ConnectionError("simulated partition")

        monkeypatch.setattr(task_store, "renew_leases", database_unreachable)
        started = time.monotonic()

        async def gave_up() -> bool:
            return not worker._inflight

        await eventually(gave_up, within=5, what="worker to abandon all work")
        assert time.monotonic() - started >= 0.5, "must not give up before a lease could lapse"
    assert "task_succeeded" not in await event_types(db, job.task_id)


async def test_demo_worker_crashes_before_reporting_if_killed_mid_task(
    engine: AsyncEngine, database_url: str, db: asyncpg.Connection
) -> None:
    """A kill that lands mid-task must stop the result from being reported."""
    job = await enqueue(engine, "sleep", {"seconds": 0.5})
    config = replace(FAST, heartbeat_interval_s=0.9, lease_ttl_s=2, demo_chaos=True)
    worker = Worker(engine=engine, registry=REGISTRY, config=config, database_url=database_url)
    exits: list[int] = []

    def fake_exit(code: int) -> None:
        exits.append(code)
        worker.request_stop()
        raise asyncio.CancelledError  # unwind this task like the process would vanish

    worker.hard_exit = fake_exit
    runner = asyncio.create_task(worker.run())
    try:
        await eventually(lambda: _is(db, job.task_id, "running"), what="task to start")
        await db.execute(
            "INSERT INTO worker_commands (worker_id, command) VALUES ($1, 'kill')", worker.id
        )
        await eventually(lambda: _exited(exits), within=5, what="the worker to crash")
    finally:
        worker.request_stop()
        await asyncio.wait_for(runner, timeout=10)
    assert exits == [137]
    assert "task_succeeded" not in await event_types(db, job.task_id), "result must not land"


async def _exited(exits: list[int]) -> bool:
    return bool(exits)

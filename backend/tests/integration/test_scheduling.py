"""Cron firing, priority aging and leader election against a real PostgreSQL."""

from __future__ import annotations

import asyncio
import contextlib
import uuid

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from conductor.core.db import asyncpg_dsn
from conductor.domain.cron import CronError
from conductor.domain.dag import WorkflowDefinition
from conductor.domain.priority import effective_priority
from conductor.scheduler.leader import LeaderElector
from conductor.store import aging as aging_store
from conductor.store import schedules as schedule_store
from conductor.store import workflows as wf_store
from conductor.worker.runtime import Worker, WorkerConfig
from tests.conftest import eventually
from tests.integration.helpers import REGISTRY, enqueue

ONE_NODE = WorkflowDefinition.model_validate({"nodes": [{"key": "a", "task_type": "sleep"}]})


async def make_scheduled(engine: AsyncEngine, cron: str = "*/5 * * * *") -> uuid.UUID:
    async with engine.begin() as conn:
        record = await wf_store.create_workflow(
            conn,
            name=f"wf-{uuid.uuid4().hex[:6]}",
            definition=ONE_NODE,
            cron=cron,
            timezone="America/New_York",
        )
    return record.id


# ------------------------------------------------------------------ schedules


async def test_saving_a_workflow_creates_and_syncs_its_schedule(
    engine: AsyncEngine, db: asyncpg.Connection
) -> None:
    workflow_id = await make_scheduled(engine)
    first = await db.fetchrow("SELECT * FROM schedules WHERE workflow_id = $1", workflow_id)
    assert first is not None
    assert first["next_fire_at"] > await db.fetchval("SELECT now()")
    assert first["next_fire_at"].minute % 5 == 0

    async with engine.begin() as conn:
        await wf_store.update_workflow(
            conn, workflow_id, definition=ONE_NODE, cron=None, timezone="UTC", enabled=True
        )
    assert await db.fetchval("SELECT count(*) FROM schedules") == 0


async def test_invalid_schedules_are_rejected_and_nothing_is_saved(
    engine: AsyncEngine, db: asyncpg.Connection
) -> None:
    with pytest.raises(CronError, match="outside 0-59"):
        async with engine.begin() as conn:
            await wf_store.create_workflow(conn, name="bad", definition=ONE_NODE, cron="61 * * * *")
    with pytest.raises(CronError, match="unknown time zone"):
        async with engine.begin() as conn:
            await wf_store.create_workflow(
                conn, name="bad", definition=ONE_NODE, cron="* * * * *", timezone="Nowhere/City"
            )
    assert await db.fetchval("SELECT count(*) FROM workflows") == 0


async def test_due_schedule_fires_once_and_skips_missed_instants(
    engine: AsyncEngine, db: asyncpg.Connection
) -> None:
    workflow_id = await make_scheduled(engine)
    # Pretend the scheduler was down for 20 minutes: 4 fire times are overdue.
    await db.execute(
        "UPDATE schedules SET next_fire_at = date_trunc('hour', now()) - interval '20 minutes'"
    )
    due_at = await db.fetchval("SELECT next_fire_at FROM schedules")

    async with engine.begin() as conn:
        [firing] = await schedule_store.fire_due_schedules(conn)
    assert firing.run_id is not None
    assert firing.scheduled_for == due_at
    assert firing.missed >= 3
    assert firing.next_fire_at > await db.fetchval("SELECT now()")

    run = await db.fetchrow("SELECT * FROM workflow_runs WHERE id = $1", firing.run_id)
    assert (run["trigger"], run["scheduled_for"], run["workflow_id"]) == (
        "schedule",
        due_at,
        workflow_id,
    )
    async with engine.begin() as conn:
        assert await schedule_store.fire_due_schedules(conn) == [], "nothing else is due"


async def test_concurrent_schedulers_fire_each_instant_once(
    engine: AsyncEngine, db: asyncpg.Connection
) -> None:
    for _ in range(10):
        await make_scheduled(engine)
    await db.execute("UPDATE schedules SET next_fire_at = now() - interval '1 second'")

    async def tick() -> int:
        async with engine.begin() as conn:
            return len(await schedule_store.fire_due_schedules(conn))

    fired = await asyncio.gather(*(tick() for _ in range(5)))
    assert sum(fired) == 10
    assert await db.fetchval("SELECT count(*) FROM workflow_runs WHERE trigger = 'schedule'") == 10


async def test_unique_index_blocks_a_duplicate_run_for_the_same_instant(
    engine: AsyncEngine, db: asyncpg.Connection
) -> None:
    await make_scheduled(engine)
    await db.execute(
        "UPDATE schedules SET next_fire_at = date_trunc('minute', now()) - interval '5 minutes'"
    )
    instant = await db.fetchval("SELECT next_fire_at FROM schedules")
    async with engine.begin() as conn:
        [first] = await schedule_store.fire_due_schedules(conn)
    # A stale scheduler that still thinks this instant is due (e.g. during failover).
    await db.execute("UPDATE schedules SET next_fire_at = $1", instant)
    async with engine.begin() as conn:
        [second] = await schedule_store.fire_due_schedules(conn)
    assert first.run_id is not None
    assert second.run_id is None
    assert await db.fetchval("SELECT count(*) FROM workflow_runs") == 1


# ------------------------------------------------------------------ aging


async def test_sql_aging_matches_the_domain_formula(
    engine: AsyncEngine, db: asyncpg.Connection
) -> None:
    waits = {"fresh": 0, "a_bit": 25, "longer": 55, "ancient": 100_000}
    priorities = {"fresh": 3, "a_bit": -2, "longer": 0, "ancient": 7}
    ids = {}
    for name in waits:
        job = await enqueue(engine, priority=priorities[name])
        ids[name] = job.task_id
        await db.execute(
            "UPDATE task_instances SET not_before = now() - make_interval(secs => $2) "
            "WHERE id = $1",
            job.task_id,
            float(waits[name]),
        )
    future = await enqueue(engine, priority=1, delay_s=60)  # not yet claimable: no aging

    async with engine.begin() as conn:
        changed = await aging_store.apply_aging(conn, interval_s=10, max_bonus=50)
    assert changed == 3  # fresh has no bonus yet, so it is not rewritten
    for name, task_id in ids.items():
        expected = effective_priority(priorities[name], waits[name], interval_s=10, max_bonus=50)
        got = await db.fetchval(
            "SELECT effective_priority FROM task_instances WHERE id = $1", task_id
        )
        assert got == expected, name
    assert (
        await db.fetchval(
            "SELECT effective_priority FROM task_instances WHERE id = $1", future.task_id
        )
        == 1
    )

    async with engine.begin() as conn:
        assert await aging_store.apply_aging(conn, interval_s=10, max_bonus=50) == 0, "idempotent"


@pytest.mark.parametrize("aging", [True, False], ids=["with-aging", "control-without-aging"])
async def test_low_priority_task_is_not_starved_by_sustained_high_priority_load(
    engine: AsyncEngine, database_url: str, db: asyncpg.Connection, aging: bool
) -> None:
    """A single worker, a never-empty queue of fresh high-priority tasks, one low task.

    With aging the low task runs within seconds; the control run (aging off)
    shows it would otherwise wait for as long as the high-priority load lasts.
    """
    low = await enqueue(engine, "sleep", {"seconds": 0}, priority=-50)
    config = WorkerConfig(
        concurrency=1, lease_ttl_s=5, heartbeat_interval_s=1, poll_interval_s=0.02, listen=False
    )
    worker = Worker(engine=engine, registry=REGISTRY, config=config, database_url=database_url)
    stop = asyncio.Event()

    async def flood() -> None:
        # Its own pooled connections: `db` is busy serving the probe below.
        while not stop.is_set():
            async with engine.connect() as conn:
                ready = (
                    await conn.execute(
                        text(
                            "SELECT count(*) FROM task_instances "
                            "WHERE status = 'ready' AND priority = 50"
                        )
                    )
                ).scalar_one()
            for _ in range(max(0, 10 - ready)):
                await enqueue(engine, "sleep", {"seconds": 0.01}, priority=50)
            await asyncio.sleep(0.01)

    async def age() -> None:
        while not stop.is_set():
            async with engine.begin() as conn:
                await aging_store.apply_aging(conn, interval_s=0.02, max_bonus=200)
            await asyncio.sleep(0.05)

    helpers = [asyncio.create_task(flood())]
    if aging:
        helpers.append(asyncio.create_task(age()))
    runner = asyncio.create_task(worker.run())
    try:

        async def low_done() -> bool:
            status = await db.fetchval(
                "SELECT status FROM task_instances WHERE id = $1", low.task_id
            )
            return bool(status == "succeeded")

        if aging:
            await eventually(low_done, within=15, what="low-priority task to run")
            backlog = await db.fetchval(
                "SELECT count(*) FROM task_instances WHERE status = 'ready' AND priority = 50"
            )
            assert backlog > 0, "it ran while high-priority work was still waiting"
        else:
            await asyncio.sleep(3)
            assert not await low_done(), "without aging, sustained load starves it"
            highs = await db.fetchval(
                "SELECT count(*) FROM task_instances WHERE status = 'succeeded' AND priority = 50"
            )
            assert highs > 20, "the worker was busy the whole time"
    finally:
        stop.set()
        worker.request_stop()
        await asyncio.gather(*helpers)
        await asyncio.wait_for(runner, timeout=10)


# ------------------------------------------------------------------ leader election


async def test_exactly_one_leader_and_failover_when_its_session_dies(
    database_url: str, db: asyncpg.Connection
) -> None:
    dsn = asyncpg_dsn(database_url)
    key = 0x7E57  # isolated from any real scheduler
    duty_runs: dict[uuid.UUID, int] = {}

    def make() -> LeaderElector:
        elector = LeaderElector(dsn=dsn, check_interval_s=0.1, retry_interval_s=0.1, key=key)

        async def duties() -> None:
            duty_runs[elector.id] = duty_runs.get(elector.id, 0) + 1
            await asyncio.Event().wait()  # lead until cancelled

        elector._duties_for_test = duties  # type: ignore[attr-defined]
        return elector

    a, b = make(), make()
    tasks = [
        asyncio.create_task(e.run(e._duties_for_test))  # type: ignore[attr-defined]
        for e in (a, b)
    ]
    try:
        first = await eventually(lambda: _leader(a, b), within=5, what="a leader to be elected")
        await asyncio.sleep(0.5)
        assert [e.is_leader for e in (a, b)].count(True) == 1, "never two leaders at once"

        # Kill the leader's database session, as a crash or partition would.
        await db.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_locks "
            "WHERE locktype = 'advisory' AND objid = $1 AND granted",
            key,
        )
        second = await eventually(
            lambda: _leader(*(e for e in (a, b) if e is not first)), within=5, what="failover"
        )
        assert second is not first

        # The old leader notices its dead session within one check interval and
        # steps down. (Until then both believe they lead: see ADR 0004 for why
        # that window is harmless.)
        async def stepped_down() -> bool:
            return not first.is_leader

        await eventually(stepped_down, within=2, what="the old leader to step down")
        recorded = await db.fetchval("SELECT leader_id FROM scheduler_state")
        assert recorded == second.id
        assert duty_runs == {first.id: 1, second.id: 1}
    finally:
        for e in (a, b):
            e.request_stop()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait_for(t, timeout=5)


async def _leader(*electors: LeaderElector) -> LeaderElector | None:
    leaders = [e for e in electors if e.is_leader]
    return leaders[0] if len(leaders) == 1 else None

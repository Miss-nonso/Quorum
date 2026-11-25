"""Kill and freeze real worker processes; prove no task is lost and no effect repeats.

These are the tests behind the README's reliability claims:

* a worker killed mid-task loses nothing: the task finishes elsewhere;
* side effects happen exactly once per idempotency key despite re-execution;
* a frozen ("zombie") worker that wakes up after losing its lease cannot
  overwrite the outcome recorded by the worker that took over;
* none of this depends on LISTEN/NOTIFY.
"""

from __future__ import annotations

import signal
import time
from collections.abc import Callable
from uuid import UUID

import asyncpg
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.chaos.conftest import Cluster
from tests.conftest import eventually
from tests.integration.helpers import FAST_RETRY, enqueue, event_types, task_row


async def _owner(db: asyncpg.Connection, task_id: UUID) -> UUID | None:
    value = await db.fetchval(
        "SELECT lease_owner FROM task_instances WHERE id = $1 AND status = 'running'", task_id
    )
    return UUID(str(value)) if value else None


async def _delivered(db: asyncpg.Connection, key: str) -> bool:
    count = await db.fetchval("SELECT count(*) FROM notifications WHERE idempotency_key = $1", key)
    return bool(count)


async def _succeeded(db: asyncpg.Connection, task_id: UUID) -> bool:
    return bool(
        await db.fetchval("SELECT status = 'succeeded' FROM task_instances WHERE id = $1", task_id)
    )


@pytest.mark.parametrize("listen", [True, False], ids=["listen", "poll-only"])
async def test_killed_worker_task_completes_elsewhere_with_effect_once(
    cluster: Cluster,
    engine: AsyncEngine,
    db: asyncpg.Connection,
    listen: bool,
    record_property: Callable[[str, object], None],
) -> None:
    cluster.extra_env["CONDUCTOR_WORKER_LISTEN"] = str(listen).lower()
    cluster.scheduler()
    victim = await cluster.worker(db, "worker-a")
    job = await enqueue(
        engine, "notify", {"message": "deploy finished", "hold_s": 2.0}, backoff=FAST_RETRY
    )

    # Kill the worker after its side effect but before it reports completion:
    # the worst moment, because the retry will re-run the effect.
    await eventually(lambda: _delivered(db, job.idempotency_key), what="first delivery")
    assert await _owner(db, job.task_id) == victim.worker_id
    survivor = await cluster.worker(db, "worker-b")
    victim.signal(signal.SIGKILL)
    killed_at = time.monotonic()

    await eventually(lambda: _succeeded(db, job.task_id), within=20, what="recovery")
    record_property("recovery_seconds", round(time.monotonic() - killed_at, 2))

    row = await task_row(db, job.task_id)
    assert row["attempt"] == 2
    assert row["result"] == '{"delivered": false, "duplicate_suppressed": true}'
    assert await db.fetchval("SELECT count(*) FROM notifications") == 1
    events = await event_types(db, job.task_id)
    assert events[events.index("lease_expired") :] == [
        "lease_expired",
        "task_retry_scheduled",
        "task_claimed",
        "task_succeeded",
    ]
    finisher = await db.fetchval(
        "SELECT payload->>'worker_id' FROM run_events "
        "WHERE task_id = $1 AND type = 'task_succeeded'",
        job.task_id,
    )
    assert finisher == str(survivor.worker_id)

    async def victim_flagged() -> bool:
        status = await db.fetchval("SELECT status FROM workers WHERE id = $1", victim.worker_id)
        return status in ("unhealthy", "dead")

    await eventually(victim_flagged, within=10, what="killed worker to be flagged")


async def test_killing_a_worker_under_load_loses_nothing_and_duplicates_nothing(
    cluster: Cluster, engine: AsyncEngine, db: asyncpg.Connection
) -> None:
    cluster.scheduler()
    workers = [await cluster.worker(db, f"worker-{n}", concurrency=3) for n in range(3)]
    jobs = [
        await enqueue(engine, "notify", {"message": f"m{n}", "hold_s": 0.3}, backoff=FAST_RETRY)
        for n in range(45)
    ]

    async def victim_holds_work() -> bool:
        held = await db.fetchval(
            "SELECT count(*) FROM task_instances WHERE status = 'running' AND lease_owner = $1",
            workers[0].worker_id,
        )
        return bool(held)

    await eventually(victim_holds_work, what="victim to hold tasks")
    workers[0].signal(signal.SIGKILL)

    async def all_succeeded() -> bool:
        done = await db.fetchval("SELECT count(*) FROM task_instances WHERE status = 'succeeded'")
        return bool(done == len(jobs))

    await eventually(all_succeeded, within=40, what="all tasks to succeed")
    assert await db.fetchval("SELECT count(*) FROM notifications") == len(jobs)
    assert await db.fetchval("SELECT count(DISTINCT idempotency_key) FROM notifications") == len(
        jobs
    )
    assert await db.fetchval("SELECT count(*) FROM run_events WHERE type = 'lease_expired'") >= 1
    assert await db.fetchval("SELECT count(*) FROM dead_letters") == 0


async def test_frozen_zombie_worker_cannot_overwrite_the_new_owners_outcome(
    cluster: Cluster, engine: AsyncEngine, db: asyncpg.Connection
) -> None:
    cluster.scheduler()
    zombie = await cluster.worker(db, "zombie")
    job = await enqueue(
        engine, "notify", {"message": "invoice sent", "hold_s": 1.0}, backoff=FAST_RETRY
    )
    await eventually(lambda: _delivered(db, job.idempotency_key), what="first delivery")

    # Freeze the process: no heartbeats, no progress. It still believes it holds the lease.
    zombie.signal(signal.SIGSTOP)
    successor = await cluster.worker(db, "successor")
    await eventually(lambda: _succeeded(db, job.task_id), within=20, what="takeover")
    winner = await task_row(db, job.task_id)

    # Thaw the zombie. Its timers are all overdue: it will either notice the
    # lost lease on its next heartbeat or try to complete. Both must be harmless.
    zombie.signal(signal.SIGCONT)

    async def zombie_reacted() -> bool:
        types = await db.fetch(
            "SELECT type FROM run_events WHERE task_id = $1 AND payload->>'worker_id' = $2",
            job.task_id,
            str(zombie.worker_id),
        )
        return bool({"lease_lost", "stale_write_rejected"} & {r["type"] for r in types})

    await eventually(zombie_reacted, within=10, what="zombie to be fenced off")

    final = await task_row(db, job.task_id)
    assert final["status"] == "succeeded"
    assert final["fencing_token"] == winner["fencing_token"] == 2
    assert final["result"] == winner["result"]
    assert final["updated_at"] == winner["updated_at"], "zombie must not have written"
    assert await db.fetchval("SELECT count(*) FROM notifications") == 1
    succeeded_by = await db.fetch(
        "SELECT payload->>'worker_id' AS w FROM run_events "
        "WHERE task_id = $1 AND type = 'task_succeeded'",
        job.task_id,
    )
    assert [r["w"] for r in succeeded_by] == [str(successor.worker_id)]


async def test_demo_kill_command_crashes_the_worker_and_its_task_recovers(
    cluster: Cluster, engine: AsyncEngine, db: asyncpg.Connection
) -> None:
    """The public demo's "kill a worker" button, end to end, with real processes."""
    from conductor.demo import service

    cluster.extra_env["CONDUCTOR_DEMO_CHAOS"] = "true"
    cluster.scheduler()
    victim = await cluster.worker(db, "victim")
    job = await enqueue(engine, "notify", {"message": "demo", "hold_s": 2.0}, backoff=FAST_RETRY)
    await eventually(lambda: _delivered(db, job.idempotency_key), what="first delivery")
    survivor = await cluster.worker(db, "survivor")

    async with engine.begin() as conn:
        killed = await service.kill_worker(conn, "chaos-test-sandbox-0001")
    assert killed.worker_id == victim.worker_id
    assert killed.tasks == ["job"]

    async def exited() -> int | None:
        return victim.popen.poll()

    assert await eventually(exited, within=10, what="victim to crash") == 137
    await eventually(lambda: _succeeded(db, job.task_id), within=20, what="recovery")
    finisher = await db.fetchval(
        "SELECT payload->>'worker_id' FROM run_events "
        "WHERE task_id = $1 AND type = 'task_succeeded'",
        job.task_id,
    )
    assert finisher == str(survivor.worker_id)
    assert await db.fetchval("SELECT count(*) FROM notifications") == 1
    types = await event_types(db, job.task_id)
    assert types.index("chaos_worker_killed") < types.index("lease_expired")

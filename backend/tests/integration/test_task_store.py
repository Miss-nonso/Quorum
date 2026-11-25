"""Queue semantics against a real PostgreSQL: claiming, leases, fencing, recovery."""

from __future__ import annotations

import asyncio
import random
import uuid
from datetime import timedelta

import asyncpg
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from quorum.domain.backoff import BackoffPolicy
from quorum.domain.retry import FailureKind
from quorum.store import tasks as task_store
from quorum.store import workers as worker_store
from quorum.store.tasks import ClaimedTask, DeadLetterNotFoundError, StaleLeaseError
from tests.integration.helpers import FAST_RETRY, enqueue, event_types, expire_lease, task_row

WORKER_A = uuid.UUID(int=0xA)
WORKER_B = uuid.UUID(int=0xB)
TTL = 30.0


async def claim(engine: AsyncEngine, worker: uuid.UUID, limit: int = 10) -> list[ClaimedTask]:
    async with engine.begin() as conn:
        return await task_store.claim_tasks(conn, worker_id=worker, limit=limit, lease_ttl_s=TTL)


async def reap(engine: AsyncEngine) -> list[task_store.ReapedTask]:
    async with engine.begin() as conn:
        return await task_store.reap_expired_leases(conn, rng=random.Random(0))


# ------------------------------------------------------------------ enqueue & claim


async def test_enqueue_creates_ready_task_with_derived_key(
    engine: AsyncEngine, db: asyncpg.Connection
) -> None:
    job = await enqueue(engine, "notify", {"message": "hi"})
    row = await task_row(db, job.task_id)
    assert row["status"] == "ready"
    assert row["attempt"] == 0
    assert row["fencing_token"] == 0
    assert row["idempotency_key"] == f"{job.run_id}:job"
    assert await event_types(db, job.task_id) == ["task_enqueued"]


async def test_duplicate_idempotency_key_is_rejected(engine: AsyncEngine) -> None:
    await enqueue(engine, idempotency_key="order-42")
    with pytest.raises(Exception, match="task_instances_idempotency_key_idx"):
        await enqueue(engine, idempotency_key="order-42")


async def test_claim_orders_by_priority_then_fifo(engine: AsyncEngine) -> None:
    low_1 = await enqueue(engine, priority=0)
    high_1 = await enqueue(engine, priority=5)
    low_2 = await enqueue(engine, priority=0)
    high_2 = await enqueue(engine, priority=5)
    claimed = await claim(engine, WORKER_A)
    assert [c.id for c in claimed] == [
        high_1.task_id,
        high_2.task_id,
        low_1.task_id,
        low_2.task_id,
    ]


async def test_claim_respects_limit_and_not_before(engine: AsyncEngine) -> None:
    await enqueue(engine, delay_s=60)
    due = [await enqueue(engine) for _ in range(3)]
    first = await claim(engine, WORKER_A, limit=2)
    second = await claim(engine, WORKER_A, limit=10)
    assert len(first) == 2
    assert {c.id for c in first + second} == {d.task_id for d in due}


async def test_claim_sets_lease_and_bumps_attempt_and_token(
    engine: AsyncEngine, db: asyncpg.Connection
) -> None:
    job = await enqueue(engine)
    [claimed] = await claim(engine, WORKER_A)
    row = await task_row(db, job.task_id)
    assert (claimed.attempt, claimed.fencing_token) == (1, 1)
    assert row["status"] == "running"
    assert row["lease_owner"] == WORKER_A
    db_now = await db.fetchval("SELECT now()")
    assert timedelta(seconds=TTL - 5) < row["lease_expires_at"] - db_now <= timedelta(seconds=TTL)
    payload = await db.fetchval(
        "SELECT payload FROM run_events WHERE task_id = $1 AND type = 'task_claimed'", job.task_id
    )
    assert f'"worker_id": "{WORKER_A}"' in payload


async def test_concurrent_claimers_never_receive_the_same_task(
    engine: AsyncEngine, db: asyncpg.Connection
) -> None:
    """20 claimers race over 400 tasks: every task is claimed exactly once."""
    await db.execute(
        """
        WITH runs AS (
            INSERT INTO workflow_runs (status) SELECT 'running' FROM generate_series(1, 400)
            RETURNING id
        )
        INSERT INTO task_instances (run_id, node_key, task_type, idempotency_key)
        SELECT id, 'job', 'sleep', id::text FROM runs
        """
    )

    async def claimer(n: int) -> list[uuid.UUID]:
        worker = uuid.UUID(int=n + 1)
        mine: list[uuid.UUID] = []
        while batch := await claim(engine, worker, limit=3):
            mine.extend(c.id for c in batch)
        return mine

    results = await asyncio.gather(*(claimer(n) for n in range(20)))
    all_claims = [task_id for result in results for task_id in result]
    assert len(all_claims) == 400
    assert len(set(all_claims)) == 400
    assert sum(1 for r in results if r) > 1, "work should be spread across claimers"
    assert await db.fetchval("SELECT count(*) FROM run_events WHERE type = 'task_claimed'") == 400
    assert await db.fetchval("SELECT max(fencing_token) FROM task_instances") == 1


# ------------------------------------------------------------------ fencing


async def test_zombie_completion_is_rejected_by_fencing_token(
    engine: AsyncEngine, db: asyncpg.Connection
) -> None:
    job = await enqueue(engine, backoff=FAST_RETRY)
    [zombie] = await claim(engine, WORKER_A)
    await expire_lease(db, job.task_id)
    await reap(engine)
    await asyncio.sleep(0.02)  # let the retry backoff pass
    [current] = await claim(engine, WORKER_B)
    assert current.fencing_token == zombie.fencing_token + 1

    async with engine.begin() as conn:
        with pytest.raises(StaleLeaseError):
            await task_store.complete_task(
                conn,
                task_id=job.task_id,
                worker_id=WORKER_A,
                fencing_token=zombie.fencing_token,
                result={"by": "zombie"},
            )
    async with engine.begin() as conn:
        await task_store.complete_task(
            conn,
            task_id=job.task_id,
            worker_id=WORKER_B,
            fencing_token=current.fencing_token,
            result={"by": "current"},
        )

    row = await task_row(db, job.task_id)
    assert row["status"] == "succeeded"
    assert row["result"] == '{"by": "current"}'
    assert "stale_write_rejected" in await event_types(db, job.task_id)


async def test_same_worker_with_old_token_is_also_rejected(
    engine: AsyncEngine, db: asyncpg.Connection
) -> None:
    """The token, not the worker id, is what proves ownership."""
    job = await enqueue(engine, backoff=FAST_RETRY)
    [first] = await claim(engine, WORKER_A)
    await expire_lease(db, job.task_id)
    await reap(engine)
    await asyncio.sleep(0.02)
    assert await claim(engine, WORKER_A)
    async with engine.begin() as conn:
        with pytest.raises(StaleLeaseError):
            await task_store.fail_task(
                conn,
                task_id=job.task_id,
                worker_id=WORKER_A,
                fencing_token=first.fencing_token,
                kind=FailureKind.ERROR,
                error={"message": "late"},
            )
    assert (await task_row(db, job.task_id))["status"] == "running"


async def test_renew_extends_only_leases_still_held(
    engine: AsyncEngine, db: asyncpg.Connection
) -> None:
    held = await enqueue(engine)
    stolen = await enqueue(engine)
    claims = {c.id: c for c in await claim(engine, WORKER_A)}
    await db.execute(
        "UPDATE task_instances SET fencing_token = fencing_token + 1, "
        "lease_expires_at = now() + interval '1 second' WHERE id = $1",
        held.task_id,
    )
    await db.execute(
        "UPDATE task_instances SET fencing_token = fencing_token + 1 WHERE id = $1",
        stolen.task_id,
    )
    # Pretend `held` was legitimately re-issued to us with the new token.
    renew_args = [(held.task_id, claims[held.task_id].fencing_token + 1)]
    renew_args.append((stolen.task_id, claims[stolen.task_id].fencing_token))
    async with engine.begin() as conn:
        renewed = await task_store.renew_leases(
            conn, worker_id=WORKER_A, held=renew_args, lease_ttl_s=TTL
        )
    assert renewed == {held.task_id}
    row = await task_row(db, held.task_id)
    assert row["lease_expires_at"] - await db.fetchval("SELECT now()") > timedelta(seconds=TTL - 5)


async def test_renew_with_nothing_held_is_a_no_op(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        assert (
            await task_store.renew_leases(conn, worker_id=WORKER_A, held=[], lease_ttl_s=TTL)
            == set()
        )


# ------------------------------------------------------------------ retries & dead letters


async def test_failure_retries_with_backoff_then_dead_letters(
    engine: AsyncEngine, db: asyncpg.Connection
) -> None:
    job = await enqueue(
        engine, max_attempts=2, backoff=BackoffPolicy(base_s=0.2, max_s=0.2, jitter="none")
    )
    [first] = await claim(engine, WORKER_A)
    async with engine.begin() as conn:
        outcome = await task_store.fail_task(
            conn,
            task_id=job.task_id,
            worker_id=WORKER_A,
            fencing_token=first.fencing_token,
            kind=FailureKind.ERROR,
            error={"message": "boom"},
        )
    assert outcome.status == "ready"
    assert outcome.delay_s == pytest.approx(0.2)
    assert await claim(engine, WORKER_A) == [], "retry must wait for its backoff"

    await asyncio.sleep(0.25)
    [second] = await claim(engine, WORKER_A)
    assert second.attempt == 2
    async with engine.begin() as conn:
        outcome = await task_store.fail_task(
            conn,
            task_id=job.task_id,
            worker_id=WORKER_A,
            fencing_token=second.fencing_token,
            kind=FailureKind.ERROR,
            error={"message": "boom again"},
        )
    assert outcome.status == "dead"

    row = await task_row(db, job.task_id)
    assert row["status"] == "dead"
    assert row["lease_owner"] is None
    letter = await db.fetchrow("SELECT * FROM dead_letters WHERE task_id = $1", job.task_id)
    assert letter is not None
    assert "attempts exhausted (2/2)" in letter["reason"]
    assert '"boom again"' in letter["payload"]
    assert await event_types(db, job.task_id) == [
        "task_enqueued",
        "task_claimed",
        "task_retry_scheduled",
        "task_claimed",
        "task_dead_lettered",
    ]


async def test_permanent_failure_skips_remaining_attempts(
    engine: AsyncEngine, db: asyncpg.Connection
) -> None:
    job = await enqueue(engine, max_attempts=5)
    [claimed] = await claim(engine, WORKER_A)
    async with engine.begin() as conn:
        outcome = await task_store.fail_task(
            conn,
            task_id=job.task_id,
            worker_id=WORKER_A,
            fencing_token=claimed.fencing_token,
            kind=FailureKind.PERMANENT,
            error={"message": "bad input"},
        )
    assert outcome.status == "dead"
    assert (await task_row(db, job.task_id))["attempt"] == 1


async def test_replay_requeues_with_fresh_budget_and_keeps_token_monotonic(
    engine: AsyncEngine, db: asyncpg.Connection
) -> None:
    job = await enqueue(engine, max_attempts=1)
    [claimed] = await claim(engine, WORKER_A)
    async with engine.begin() as conn:
        await task_store.fail_task(
            conn,
            task_id=job.task_id,
            worker_id=WORKER_A,
            fencing_token=claimed.fencing_token,
            kind=FailureKind.ERROR,
            error={"message": "x"},
        )
    letter_id = await db.fetchval("SELECT id FROM dead_letters WHERE task_id = $1", job.task_id)

    async with engine.begin() as conn:
        assert await task_store.replay_dead_letter(conn, letter_id) == job.task_id
    row = await task_row(db, job.task_id)
    assert (row["status"], row["attempt"], row["fencing_token"]) == ("ready", 0, 1)
    assert await db.fetchval("SELECT replayed_at FROM dead_letters WHERE id = $1", letter_id)

    [again] = await claim(engine, WORKER_B)
    assert (again.attempt, again.fencing_token) == (1, 2)

    async with engine.begin() as conn:
        with pytest.raises(DeadLetterNotFoundError):
            await task_store.replay_dead_letter(conn, letter_id)


# ------------------------------------------------------------------ reaper


async def test_reaper_requeues_expired_leases_and_leaves_live_ones(
    engine: AsyncEngine, db: asyncpg.Connection
) -> None:
    expired = await enqueue(engine, backoff=FAST_RETRY)
    live = await enqueue(engine)
    await claim(engine, WORKER_A)
    await expire_lease(db, expired.task_id)

    reaped = await reap(engine)

    assert [(r.task_id, r.previous_owner) for r in reaped] == [(expired.task_id, WORKER_A)]
    assert (await task_row(db, expired.task_id))["status"] == "ready"
    assert (await task_row(db, live.task_id))["status"] == "running"
    assert await event_types(db, expired.task_id) == [
        "task_enqueued",
        "task_claimed",
        "lease_expired",
        "task_retry_scheduled",
    ]


async def test_reaper_dead_letters_a_task_that_keeps_killing_workers(
    engine: AsyncEngine, db: asyncpg.Connection
) -> None:
    job = await enqueue(engine, max_attempts=2, backoff=FAST_RETRY)
    for _ in range(2):
        await asyncio.sleep(0.02)
        await claim(engine, WORKER_A)
        await expire_lease(db, job.task_id)
        await reap(engine)
    row = await task_row(db, job.task_id)
    assert row["status"] == "dead"
    reason = await db.fetchval("SELECT reason FROM dead_letters WHERE task_id = $1", job.task_id)
    assert reason == "lease_expired: attempts exhausted (2/2)"


async def test_concurrent_reapers_recover_each_task_exactly_once(
    engine: AsyncEngine, db: asyncpg.Connection
) -> None:
    jobs = [await enqueue(engine, backoff=FAST_RETRY) for _ in range(50)]
    await claim(engine, WORKER_A, limit=50)
    await db.execute(
        "UPDATE task_instances SET lease_expires_at = now() - interval '1 second' "
        "WHERE status = 'running'"
    )

    async def reaper() -> int:
        total = 0
        while batch := await reap(engine):
            total += len(batch)
        return total

    counts = await asyncio.gather(*(reaper() for _ in range(5)))
    assert sum(counts) == len(jobs)
    assert await db.fetchval("SELECT count(*) FROM run_events WHERE type = 'lease_expired'") == 50
    assert await db.fetchval("SELECT count(*) FROM task_instances WHERE status = 'ready'") == 50


async def test_lease_renewed_at_the_last_moment_is_not_reaped(
    engine: AsyncEngine, db: asyncpg.Connection
) -> None:
    job = await enqueue(engine)
    [claimed] = await claim(engine, WORKER_A)
    await expire_lease(db, job.task_id)
    async with engine.begin() as conn:
        renewed = await task_store.renew_leases(
            conn,
            worker_id=WORKER_A,
            held=[(job.task_id, claimed.fencing_token)],
            lease_ttl_s=TTL,
        )
    assert renewed == {job.task_id}
    assert await reap(engine) == []


# ------------------------------------------------------------------ release & workers


async def test_release_returns_task_without_spending_an_attempt(
    engine: AsyncEngine, db: asyncpg.Connection
) -> None:
    job = await enqueue(engine)
    [claimed] = await claim(engine, WORKER_A)
    async with engine.begin() as conn:
        assert await task_store.release_task(
            conn, task_id=job.task_id, worker_id=WORKER_A, fencing_token=claimed.fencing_token
        )
        assert not await task_store.release_task(
            conn, task_id=job.task_id, worker_id=WORKER_A, fencing_token=claimed.fencing_token
        )
    row = await task_row(db, job.task_id)
    assert (row["status"], row["attempt"], row["fencing_token"]) == ("ready", 0, 1)
    [again] = await claim(engine, WORKER_B)
    assert (again.attempt, again.fencing_token) == (1, 2)


async def test_silent_workers_become_unhealthy_then_dead(
    engine: AsyncEngine, db: asyncpg.Connection
) -> None:
    async with engine.begin() as conn:
        for worker in (WORKER_A, WORKER_B):
            await worker_store.register_worker(
                conn, worker_id=worker, hostname="h", pid=1, concurrency=1, capabilities=[]
            )
    await db.execute(
        "UPDATE workers SET last_heartbeat_at = now() - interval '20 seconds' WHERE id = $1",
        WORKER_A,
    )
    async with engine.begin() as conn:
        changes = await worker_store.mark_stale_workers(conn, unhealthy_after_s=10, dead_after_s=60)
    assert [(c.worker_id, c.status) for c in changes] == [(WORKER_A, "unhealthy")]

    await db.execute(
        "UPDATE workers SET last_heartbeat_at = now() - interval '90 seconds' WHERE id = $1",
        WORKER_A,
    )
    async with engine.begin() as conn:
        changes = await worker_store.mark_stale_workers(conn, unhealthy_after_s=10, dead_after_s=60)
        assert [(c.worker_id, c.status) for c in changes] == [(WORKER_A, "dead")]
        await worker_store.heartbeat(conn, worker_id=WORKER_A, current_load=0)
    assert await db.fetchval("SELECT status FROM workers WHERE id = $1", WORKER_A) == "active"

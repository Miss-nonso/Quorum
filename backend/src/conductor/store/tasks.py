"""Task queue operations: enqueue, claim, renew, complete, fail, reap, replay.

The safety argument, in brief (full version in docs/semantics.md):

* **Claiming** uses ``FOR UPDATE SKIP LOCKED`` inside a single statement, so
  two workers can never claim the same ``ready`` row.
* **Leases** are timestamps computed by the database clock (``now()``), so
  worker clock skew cannot extend or shorten them.
* **Fencing tokens** increase on every claim and never reset. Every write a
  worker makes is conditional on ``(status='running', lease_owner, fencing_token)``
  still matching; a worker whose lease was taken over (a "zombie") has a
  stale token, so its writes match zero rows and are rejected.
"""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import RowMapping, bindparam, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncConnection

from conductor.domain.backoff import BackoffPolicy
from conductor.domain.idempotency import derive_idempotency_key, validate_idempotency_key
from conductor.domain.retry import DeadLetter, FailureKind, Requeue, decide_after_failure
from conductor.domain.task_state import TaskStatus, ensure_transition
from conductor.store.events import EventType, notify_task_ready, record_event

JOB_NODE_KEY = "job"


class StaleLeaseError(Exception):
    """A worker tried to write to a task whose lease it no longer holds."""

    def __init__(self, task_id: UUID, fencing_token: int) -> None:
        super().__init__(f"lease on task {task_id} (token {fencing_token}) is no longer held")
        self.task_id = task_id
        self.fencing_token = fencing_token


class DeadLetterNotFoundError(LookupError):
    """No open (un-replayed) dead letter has the given id."""


@dataclass(frozen=True, slots=True)
class TaskSpec:
    """Everything needed to enqueue one task. Params must already be validated."""

    task_type: str
    params: Mapping[str, Any] = field(default_factory=dict)
    priority: int = 0
    max_attempts: int = 3
    backoff: BackoffPolicy = field(default_factory=BackoffPolicy)
    timeout_s: float = 60.0
    idempotency_key: str | None = None
    delay_s: float = 0.0


@dataclass(frozen=True, slots=True)
class EnqueuedTask:
    run_id: UUID
    task_id: UUID
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class ClaimedTask:
    """A task leased to a worker. ``fencing_token`` must accompany every write."""

    id: UUID
    run_id: UUID
    node_key: str
    task_type: str
    params: dict[str, Any]
    attempt: int
    max_attempts: int
    fencing_token: int
    idempotency_key: str
    timeout_s: float
    lease_expires_at: datetime


@dataclass(frozen=True, slots=True)
class FailureOutcome:
    """What the store did with a failed attempt."""

    task_id: UUID
    status: TaskStatus  # READY (retry scheduled) or DEAD
    delay_s: float | None = None
    reason: str | None = None


# --------------------------------------------------------------------- enqueue


async def enqueue_job(conn: AsyncConnection, spec: TaskSpec) -> EnqueuedTask:
    """Create a standalone job: a one-node run with no workflow."""
    run_id = uuid4()
    task_id = uuid4()
    key = (
        validate_idempotency_key(spec.idempotency_key)
        if spec.idempotency_key
        else derive_idempotency_key(run_id, JOB_NODE_KEY)
    )
    await conn.execute(
        text(
            "INSERT INTO workflow_runs (id, status, trigger, started_at) "
            "VALUES (:id, 'running', 'api', now())"
        ),
        {"id": run_id},
    )
    await conn.execute(
        text(
            """
            INSERT INTO task_instances (
                id, run_id, node_key, task_type, params, status, priority,
                effective_priority, max_attempts, backoff_policy, timeout_s,
                not_before, idempotency_key
            ) VALUES (
                :id, :run_id, :node_key, :task_type, :params, 'ready', :priority,
                :priority, :max_attempts, :backoff, :timeout_s,
                now() + make_interval(secs => :delay_s), :key
            )
            """
        ).bindparams(bindparam("params", type_=JSONB), bindparam("backoff", type_=JSONB)),
        {
            "id": task_id,
            "run_id": run_id,
            "node_key": JOB_NODE_KEY,
            "task_type": spec.task_type,
            "params": dict(spec.params),
            "priority": spec.priority,
            "max_attempts": spec.max_attempts,
            "backoff": spec.backoff.model_dump(),
            "timeout_s": spec.timeout_s,
            "delay_s": spec.delay_s,
            "key": key,
        },
    )
    await record_event(
        conn,
        run_id=run_id,
        task_id=task_id,
        type=EventType.TASK_ENQUEUED,
        payload={"task_type": spec.task_type, "priority": spec.priority},
    )
    await notify_task_ready(conn)
    return EnqueuedTask(run_id=run_id, task_id=task_id, idempotency_key=key)


# --------------------------------------------------------------------- claim

_CLAIM = text(
    """
    WITH candidates AS (
        SELECT id
        FROM task_instances
        WHERE status = 'ready' AND not_before <= now()
        ORDER BY effective_priority DESC, created_at
        LIMIT :limit
        FOR UPDATE SKIP LOCKED
    ),
    claimed AS (
        UPDATE task_instances t
        SET status = 'running',
            lease_owner = :worker_id,
            lease_expires_at = now() + make_interval(secs => :lease_ttl_s),
            fencing_token = t.fencing_token + 1,
            attempt = t.attempt + 1,
            updated_at = now()
        FROM candidates c
        WHERE t.id = c.id
        RETURNING t.id, t.run_id, t.node_key, t.task_type, t.params, t.attempt,
                  t.max_attempts, t.fencing_token, t.idempotency_key, t.timeout_s,
                  t.lease_owner, t.lease_expires_at, t.effective_priority, t.created_at
    ),
    -- Data-modifying CTEs always run, even though the final SELECT ignores this one.
    events AS (
        INSERT INTO run_events (run_id, task_id, type, payload)
        SELECT run_id, id, 'task_claimed',
               jsonb_build_object('worker_id', lease_owner,
                                  'attempt', attempt, 'fencing_token', fencing_token)
        FROM claimed
    )
    SELECT * FROM claimed ORDER BY effective_priority DESC, created_at
    """
)


async def claim_tasks(
    conn: AsyncConnection, *, worker_id: UUID, limit: int, lease_ttl_s: float
) -> list[ClaimedTask]:
    """Atomically lease up to ``limit`` ready tasks to ``worker_id``.

    Highest effective priority first, FIFO within a priority. Rows locked by
    a concurrent claimer are skipped, not waited on, so claimers never block
    one another and never receive the same row.
    """
    if limit < 1:
        return []
    rows = (
        await conn.execute(
            _CLAIM, {"worker_id": worker_id, "limit": limit, "lease_ttl_s": lease_ttl_s}
        )
    ).mappings()
    return [
        ClaimedTask(
            id=r["id"],
            run_id=r["run_id"],
            node_key=r["node_key"],
            task_type=r["task_type"],
            params=r["params"],
            attempt=r["attempt"],
            max_attempts=r["max_attempts"],
            fencing_token=r["fencing_token"],
            idempotency_key=r["idempotency_key"],
            timeout_s=r["timeout_s"],
            lease_expires_at=r["lease_expires_at"],
        )
        for r in rows
    ]


# --------------------------------------------------------------------- renew

_RENEW = text(
    """
    UPDATE task_instances t
    SET lease_expires_at = now() + make_interval(secs => :lease_ttl_s), updated_at = now()
    FROM unnest(CAST(:ids AS uuid[]), CAST(:tokens AS bigint[])) AS held(id, token)
    WHERE t.id = held.id
      AND t.fencing_token = held.token
      AND t.status = 'running'
      AND t.lease_owner = :worker_id
    RETURNING t.id
    """
)


async def renew_leases(
    conn: AsyncConnection,
    *,
    worker_id: UUID,
    held: Sequence[tuple[UUID, int]],
    lease_ttl_s: float,
) -> set[UUID]:
    """Extend the leases ``worker_id`` still holds; return the ids renewed.

    A held task missing from the result has been taken over (or finished by
    someone else): the worker must stop working on it. Renewing a lease that
    has lapsed but not yet been reaped is safe and allowed: its token still
    matches, so nobody else can hold it.
    """
    if not held:
        return set()
    result = await conn.execute(
        _RENEW,
        {
            "worker_id": worker_id,
            "ids": [task_id for task_id, _ in held],
            "tokens": [token for _, token in held],
            "lease_ttl_s": lease_ttl_s,
        },
    )
    return {row.id for row in result}


# --------------------------------------------------------------------- complete


async def _reject_stale_write(
    conn: AsyncConnection, *, task_id: UUID, worker_id: UUID, fencing_token: int, action: str
) -> StaleLeaseError:
    run_id = (
        await conn.execute(
            text("SELECT run_id FROM task_instances WHERE id = :id"), {"id": task_id}
        )
    ).scalar_one_or_none()
    if run_id is not None:
        await record_event(
            conn,
            run_id=run_id,
            task_id=task_id,
            type=EventType.STALE_WRITE_REJECTED,
            payload={"worker_id": str(worker_id), "fencing_token": fencing_token, "action": action},
        )
    return StaleLeaseError(task_id, fencing_token)


async def complete_task(
    conn: AsyncConnection,
    *,
    task_id: UUID,
    worker_id: UUID,
    fencing_token: int,
    result: Mapping[str, Any],
) -> None:
    """Mark a task succeeded, if and only if the caller still holds its lease.

    Raises:
        StaleLeaseError: the lease was lost; the result is discarded and a
            ``stale_write_rejected`` event is recorded instead.
    """
    ensure_transition(TaskStatus.RUNNING, TaskStatus.SUCCEEDED)
    row = (
        await conn.execute(
            text(
                """
                UPDATE task_instances
                SET status = 'succeeded', result = :result, error = NULL,
                    lease_owner = NULL, lease_expires_at = NULL, updated_at = now()
                WHERE id = :id AND status = 'running'
                  AND lease_owner = :worker_id AND fencing_token = :token
                RETURNING run_id, attempt
                """
            ).bindparams(bindparam("result", type_=JSONB)),
            {"id": task_id, "worker_id": worker_id, "token": fencing_token, "result": dict(result)},
        )
    ).one_or_none()
    if row is None:
        raise await _reject_stale_write(
            conn,
            task_id=task_id,
            worker_id=worker_id,
            fencing_token=fencing_token,
            action="complete",
        )
    await record_event(
        conn,
        run_id=row.run_id,
        task_id=task_id,
        type=EventType.TASK_SUCCEEDED,
        payload={"worker_id": str(worker_id), "attempt": row.attempt},
    )


# --------------------------------------------------------------------- failure

_LOCK_FOR_FAILURE = """
    SELECT id, run_id, attempt, max_attempts, backoff_policy, params, lease_owner, fencing_token
    FROM task_instances
"""


async def _apply_failure(
    conn: AsyncConnection,
    row: RowMapping,
    *,
    kind: FailureKind,
    error: Mapping[str, Any],
    rng: random.Random,
) -> FailureOutcome:
    """Requeue with backoff or dead-letter a locked, running task row."""
    decision = decide_after_failure(
        attempt=row["attempt"],
        max_attempts=row["max_attempts"],
        kind=kind,
        policy=BackoffPolicy.model_validate(row["backoff_policy"]),
        rng=rng,
    )
    error_doc = {"kind": kind.value, "attempt": row["attempt"], **error}

    if isinstance(decision, Requeue):
        ensure_transition(TaskStatus.RUNNING, TaskStatus.READY)
        await conn.execute(
            text(
                """
                UPDATE task_instances
                SET status = 'ready', error = :error, lease_owner = NULL, lease_expires_at = NULL,
                    not_before = now() + make_interval(secs => :delay_s), updated_at = now()
                WHERE id = :id
                """
            ).bindparams(bindparam("error", type_=JSONB)),
            {"id": row["id"], "error": error_doc, "delay_s": decision.delay_s},
        )
        await record_event(
            conn,
            run_id=row["run_id"],
            task_id=row["id"],
            type=EventType.TASK_RETRY_SCHEDULED,
            payload={
                "attempt": row["attempt"],
                "max_attempts": row["max_attempts"],
                "delay_s": round(decision.delay_s, 3),
                "error": error_doc,
            },
        )
        await notify_task_ready(conn)
        return FailureOutcome(task_id=row["id"], status=TaskStatus.READY, delay_s=decision.delay_s)

    assert isinstance(decision, DeadLetter)  # noqa: S101 - exhaustive union
    ensure_transition(TaskStatus.RUNNING, TaskStatus.DEAD)
    await conn.execute(
        text(
            """
            UPDATE task_instances
            SET status = 'dead', error = :error, lease_owner = NULL, lease_expires_at = NULL,
                updated_at = now()
            WHERE id = :id
            """
        ).bindparams(bindparam("error", type_=JSONB)),
        {"id": row["id"], "error": error_doc},
    )
    dead_letter_id = (
        await conn.execute(
            text(
                "INSERT INTO dead_letters (task_id, reason, payload) "
                "VALUES (:task_id, :reason, :payload) RETURNING id"
            ).bindparams(bindparam("payload", type_=JSONB)),
            {
                "task_id": row["id"],
                "reason": decision.reason,
                "payload": {"params": row["params"], "error": error_doc},
            },
        )
    ).scalar_one()
    await record_event(
        conn,
        run_id=row["run_id"],
        task_id=row["id"],
        type=EventType.TASK_DEAD_LETTERED,
        payload={"reason": decision.reason, "dead_letter_id": str(dead_letter_id)},
    )
    return FailureOutcome(task_id=row["id"], status=TaskStatus.DEAD, reason=decision.reason)


async def fail_task(
    conn: AsyncConnection,
    *,
    task_id: UUID,
    worker_id: UUID,
    fencing_token: int,
    kind: FailureKind,
    error: Mapping[str, Any],
    rng: random.Random | None = None,
) -> FailureOutcome:
    """Record a failed attempt by the lease holder: retry with backoff or dead-letter.

    Raises:
        StaleLeaseError: the caller no longer holds the lease.
    """
    row = (
        (
            await conn.execute(
                text(
                    _LOCK_FOR_FAILURE
                    + """
                WHERE id = :id AND status = 'running'
                  AND lease_owner = :worker_id AND fencing_token = :token
                FOR UPDATE
                """
                ),
                {"id": task_id, "worker_id": worker_id, "token": fencing_token},
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise await _reject_stale_write(
            conn, task_id=task_id, worker_id=worker_id, fencing_token=fencing_token, action="fail"
        )
    return await _apply_failure(conn, row, kind=kind, error=error, rng=rng or random.Random())


async def release_task(
    conn: AsyncConnection, *, task_id: UUID, worker_id: UUID, fencing_token: int
) -> bool:
    """Hand a task back without counting the attempt (graceful worker shutdown).

    Returns False if the lease was already lost, in which case there is
    nothing to release. The fencing token is *not* reset, so the releasing
    worker can never write to the task again.
    """
    ensure_transition(TaskStatus.RUNNING, TaskStatus.READY)
    row = (
        await conn.execute(
            text(
                """
                UPDATE task_instances
                SET status = 'ready', attempt = attempt - 1, lease_owner = NULL,
                    lease_expires_at = NULL, not_before = now(), updated_at = now()
                WHERE id = :id AND status = 'running'
                  AND lease_owner = :worker_id AND fencing_token = :token
                RETURNING run_id
                """
            ),
            {"id": task_id, "worker_id": worker_id, "token": fencing_token},
        )
    ).one_or_none()
    if row is None:
        return False
    await record_event(
        conn,
        run_id=row.run_id,
        task_id=task_id,
        type=EventType.TASK_RELEASED,
        payload={"worker_id": str(worker_id)},
    )
    await notify_task_ready(conn)
    return True


# --------------------------------------------------------------------- reaper


@dataclass(frozen=True, slots=True)
class ReapedTask:
    task_id: UUID
    previous_owner: UUID
    outcome: FailureOutcome


async def reap_expired_leases(
    conn: AsyncConnection, *, limit: int = 100, rng: random.Random | None = None
) -> list[ReapedTask]:
    """Recover running tasks whose lease has lapsed (dead, stalled or partitioned worker).

    Each is requeued with backoff, or dead-lettered if its attempts are spent.
    Safe to run concurrently from several processes: rows are locked with
    SKIP LOCKED and the expiry condition is re-checked under the lock, so a
    lease renewed at the last moment is left alone and no task is reaped twice.
    """
    rng = rng or random.Random()
    rows = (
        (
            await conn.execute(
                text(
                    _LOCK_FOR_FAILURE
                    + """
                WHERE status = 'running' AND lease_expires_at < now()
                ORDER BY lease_expires_at
                LIMIT :limit
                FOR UPDATE SKIP LOCKED
                """
                ),
                {"limit": limit},
            )
        )
        .mappings()
        .all()
    )

    reaped: list[ReapedTask] = []
    for row in rows:
        await record_event(
            conn,
            run_id=row["run_id"],
            task_id=row["id"],
            type=EventType.LEASE_EXPIRED,
            payload={
                "worker_id": str(row["lease_owner"]),
                "attempt": row["attempt"],
                "fencing_token": row["fencing_token"],
            },
        )
        outcome = await _apply_failure(
            conn,
            row,
            kind=FailureKind.LEASE_EXPIRED,
            error={"message": "worker stopped heartbeating before the task finished"},
            rng=rng,
        )
        reaped.append(ReapedTask(row["id"], row["lease_owner"], outcome))
    return reaped


# --------------------------------------------------------------------- replay


async def replay_dead_letter(conn: AsyncConnection, dead_letter_id: UUID) -> UUID:
    """Return a dead-lettered task to the queue with a fresh attempt budget.

    The idempotency key is unchanged, so a side effect that already happened
    before the task died is not repeated by the replay.

    Raises:
        DeadLetterNotFoundError: unknown id, or already replayed.
    """
    task_id = (
        await conn.execute(
            text(
                "UPDATE dead_letters SET replayed_at = now() "
                "WHERE id = :id AND replayed_at IS NULL RETURNING task_id"
            ),
            {"id": dead_letter_id},
        )
    ).scalar_one_or_none()
    if task_id is None:
        raise DeadLetterNotFoundError(f"no open dead letter {dead_letter_id}")

    ensure_transition(TaskStatus.DEAD, TaskStatus.READY)
    run_id = (
        await conn.execute(
            text(
                """
                UPDATE task_instances
                SET status = 'ready', attempt = 0, not_before = now(), updated_at = now()
                WHERE id = :id AND status = 'dead'
                RETURNING run_id
                """
            ),
            {"id": task_id},
        )
    ).scalar_one()
    await record_event(
        conn,
        run_id=run_id,
        task_id=task_id,
        type=EventType.TASK_REPLAYED,
        payload={"dead_letter_id": str(dead_letter_id)},
    )
    await notify_task_ready(conn)
    return UUID(str(task_id))

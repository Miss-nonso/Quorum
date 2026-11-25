"""Workflow runs: materialisation, dependency advancement, status, cancellation.

**Lock order.** Every transaction that changes a task's outcome locks the
run row *first* (``SELECT … FOR UPDATE``), then the task row. That does two
jobs:

1. It serialises outcome changes within one run, which closes the classic
   race where two parents of the same child finish concurrently, each sees
   the other as unfinished, and the child is never released. With the run
   lock, the second transaction's statements see the first one's commit.
2. A single global order (run, then task) means these transactions cannot
   deadlock with each other. The reaper takes run locks with SKIP LOCKED,
   so it never waits on a worker at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncConnection

from quorum.domain.dag import WorkflowDefinition, validate_definition
from quorum.domain.idempotency import derive_idempotency_key
from quorum.domain.run_state import RunStatus, derive_run_status, ensure_run_transition
from quorum.domain.task_state import TaskStatus
from quorum.store.events import EventType, notify_task_ready, record_event


class RunNotFoundError(LookupError):
    pass


class RunNotCancellableError(ValueError):
    pass


async def lock_run(conn: AsyncConnection, run_id: UUID, *, skip_locked: bool = False) -> str | None:
    """Lock a run row for the rest of the transaction; return its status.

    With ``skip_locked`` returns ``None`` instead of waiting if another
    transaction holds the lock (or the run does not exist).
    """
    suffix = "FOR UPDATE SKIP LOCKED" if skip_locked else "FOR UPDATE"
    status: str | None = (
        await conn.execute(
            # `suffix` is one of two constants above, never input.
            text(f"SELECT status FROM workflow_runs WHERE id = :id {suffix}"),  # noqa: S608
            {"id": run_id},
        )
    ).scalar_one_or_none()
    return status


async def run_of_task(conn: AsyncConnection, task_id: UUID) -> UUID | None:
    run_id: UUID | None = (
        await conn.execute(
            text("SELECT run_id FROM task_instances WHERE id = :id"), {"id": task_id}
        )
    ).scalar_one_or_none()
    return run_id


# ------------------------------------------------------------------ materialise


@dataclass(frozen=True, slots=True)
class CreatedRun:
    run_id: UUID
    task_ids: dict[str, UUID]


async def create_workflow_run(
    conn: AsyncConnection,
    workflow_id: UUID,
    *,
    trigger: str = "manual",
    scheduled_for: datetime | None = None,
    sandbox_id: str | None = None,
    chaos: dict[str, dict[str, Any]] | None = None,
) -> CreatedRun:
    """Materialise a run: one task per node, roots ready, the rest pending.

    The definition was validated when saved; it is re-checked here (cheaply,
    structurally) so a row edited behind the API's back can't create a
    broken run.

    Raises:
        RunNotFoundError: the workflow does not exist.
    """
    row = (
        await conn.execute(
            text("SELECT definition, version FROM workflows WHERE id = :id"), {"id": workflow_id}
        )
    ).one_or_none()
    if row is None:
        raise RunNotFoundError(f"no workflow {workflow_id}")
    definition = WorkflowDefinition.model_validate(row.definition)
    order = validate_definition(definition)

    run_id = uuid4()
    await conn.execute(
        text(
            """
            INSERT INTO workflow_runs
                (id, workflow_id, version, status, trigger, scheduled_for, started_at, sandbox_id)
            VALUES (:id, :wf, :version, 'running', :trigger, :scheduled_for, now(), :sandbox)
            """
        ),
        {
            "id": run_id,
            "wf": workflow_id,
            "version": row.version,
            "trigger": trigger,
            "scheduled_for": scheduled_for,
            "sandbox": sandbox_id,
        },
    )
    task_ids = {key: uuid4() for key in order}
    insert_task = text(
        """
        INSERT INTO task_instances (
            id, run_id, node_key, task_type, params, status, priority, effective_priority,
            max_attempts, backoff_policy, timeout_s, failure_policy, idempotency_key, chaos
        ) VALUES (
            :id, :run_id, :key, :task_type, :params, :status, :priority, :priority,
            :max_attempts, :backoff, :timeout_s, :failure_policy, :idempotency_key, :chaos
        )
        """
    ).bindparams(
        bindparam("params", type_=JSONB),
        bindparam("backoff", type_=JSONB),
        bindparam("chaos", type_=JSONB, required=False),
    )
    for key in order:
        node = definition.node(key)
        await conn.execute(
            insert_task,
            {
                "id": task_ids[key],
                "run_id": run_id,
                "key": key,
                "task_type": node.task_type,
                "params": node.params,
                "status": TaskStatus.PENDING if node.depends_on else TaskStatus.READY,
                "priority": node.priority,
                "max_attempts": node.max_attempts,
                "backoff": node.backoff.model_dump(),
                "timeout_s": node.timeout_s,
                "failure_policy": node.failure_policy.value,
                "idempotency_key": derive_idempotency_key(run_id, key),
                "chaos": (chaos or {}).get(key),
            },
        )
    edges = [
        {"task": task_ids[n.key], "dep": task_ids[d]}
        for n in definition.nodes
        for d in n.depends_on
    ]
    if edges:
        await conn.execute(
            text(
                "INSERT INTO task_dependencies (task_id, depends_on_task_id) VALUES (:task, :dep)"
            ),
            edges,
        )
    await record_event(
        conn,
        run_id=run_id,
        task_id=None,
        type=EventType.RUN_CREATED,
        payload={
            "workflow_id": str(workflow_id),
            "version": row.version,
            "trigger": trigger,
            "nodes": len(order),
        },
    )
    for key in order:
        if not definition.node(key).depends_on:
            await record_event(
                conn, run_id=run_id, task_id=task_ids[key], type=EventType.TASK_READY, payload={}
            )
    await notify_task_ready(conn)
    return CreatedRun(run_id=run_id, task_ids=task_ids)


# ------------------------------------------------------------------ advance


async def release_dependents(conn: AsyncConnection, *, run_id: UUID, task_id: UUID) -> list[UUID]:
    """Mark pending dependents of ``task_id`` ready if all their dependencies are satisfied.

    Call with the run locked, after ``task_id`` reached a satisfying state.
    """
    rows = await conn.execute(
        text(
            """
            UPDATE task_instances t
            SET status = 'ready', not_before = now(), updated_at = now()
            WHERE t.run_id = :run_id AND t.status = 'pending'
              AND EXISTS (SELECT 1 FROM task_dependencies d
                          WHERE d.task_id = t.id AND d.depends_on_task_id = :task_id)
              AND NOT EXISTS (
                    SELECT 1 FROM task_dependencies d
                    JOIN task_instances p ON p.id = d.depends_on_task_id
                    WHERE d.task_id = t.id
                      -- satisfied = succeeded, or failed under the `continue` policy
                      AND NOT (p.status = 'succeeded'
                               OR (p.status = 'failed' AND p.failure_policy = 'continue')))
            RETURNING t.id, t.node_key
            """
        ),
        {"run_id": run_id, "task_id": task_id},
    )
    released = [(r.id, r.node_key) for r in rows]
    for dependent, _ in released:
        await record_event(
            conn,
            run_id=run_id,
            task_id=dependent,
            type=EventType.TASK_READY,
            payload={"unblocked_by": str(task_id)},
        )
    if released:
        await notify_task_ready(conn)
    return [dependent for dependent, _ in released]


async def refresh_run_status(conn: AsyncConnection, run_id: UUID) -> RunStatus:
    """Bring the run's status in line with its tasks. Call with the run locked."""
    current = RunStatus(
        (
            await conn.execute(
                text("SELECT status FROM workflow_runs WHERE id = :id"), {"id": run_id}
            )
        ).scalar_one()
    )
    if current is RunStatus.CANCELLED:
        return current
    counts = {
        TaskStatus(r.status): r.n
        for r in await conn.execute(
            text(
                "SELECT status, count(*) AS n FROM task_instances "
                "WHERE run_id = :id GROUP BY status"
            ),
            {"id": run_id},
        )
    }
    target = derive_run_status(counts)
    if target is current:
        return current
    ensure_run_transition(current, target)
    finished = target in (RunStatus.SUCCEEDED, RunStatus.FAILED)
    await conn.execute(
        text(
            "UPDATE workflow_runs SET status = :status, "
            "finished_at = CASE WHEN :finished THEN now() ELSE NULL END WHERE id = :id"
        ),
        {"id": run_id, "status": target.value, "finished": finished},
    )
    event = {
        RunStatus.SUCCEEDED: EventType.RUN_SUCCEEDED,
        RunStatus.FAILED: EventType.RUN_FAILED,
        RunStatus.RUNNING: EventType.RUN_RESUMED,
    }[target]
    await record_event(
        conn,
        run_id=run_id,
        task_id=None,
        type=event,
        payload={"tasks": {k.value: v for k, v in counts.items()}},
    )
    return target


async def cancel_open_tasks(conn: AsyncConnection, *, run_id: UUID, reason: str) -> list[UUID]:
    """Cancel every pending, ready or running task in the run. Call with the run locked.

    A running task's lease is cleared: its worker's next renewal or write
    will not match (status is no longer 'running'), so it stops and its
    result is discarded.
    """
    rows = await conn.execute(
        text(
            """
            UPDATE task_instances
            SET status = 'cancelled', lease_owner = NULL, lease_expires_at = NULL,
                updated_at = now()
            WHERE run_id = :run_id AND status IN ('pending', 'ready', 'running')
            RETURNING id
            """
        ),
        {"run_id": run_id},
    )
    cancelled = [r.id for r in rows]
    for task_id in cancelled:
        await record_event(
            conn,
            run_id=run_id,
            task_id=task_id,
            type=EventType.TASK_CANCELLED,
            payload={"reason": reason},
        )
    return cancelled


async def cancel_run(conn: AsyncConnection, run_id: UUID) -> list[UUID]:
    """Cancel a run and its unfinished tasks. Returns the cancelled task ids.

    Raises:
        RunNotFoundError: no such run.
        RunNotCancellableError: the run already finished.
    """
    status = await lock_run(conn, run_id)
    if status is None:
        raise RunNotFoundError(f"no run {run_id}")
    if RunStatus(status) not in (RunStatus.PENDING, RunStatus.RUNNING, RunStatus.FAILED):
        raise RunNotCancellableError(f"run {run_id} is already {status}")
    ensure_run_transition(RunStatus(status), RunStatus.CANCELLED)
    cancelled = await cancel_open_tasks(conn, run_id=run_id, reason="run cancelled")
    await conn.execute(
        text("UPDATE workflow_runs SET status = 'cancelled', finished_at = now() WHERE id = :id"),
        {"id": run_id},
    )
    await record_event(
        conn,
        run_id=run_id,
        task_id=None,
        type=EventType.RUN_CANCELLED,
        payload={"cancelled_tasks": len(cancelled)},
    )
    return cancelled


async def upstream_results(conn: AsyncConnection, run_id: UUID) -> dict[str, Any]:
    """Results of the run's succeeded tasks, by node key (for param references)."""
    rows = await conn.execute(
        text(
            "SELECT node_key, result FROM task_instances "
            "WHERE run_id = :id AND status = 'succeeded'"
        ),
        {"id": run_id},
    )
    return {r.node_key: r.result for r in rows}

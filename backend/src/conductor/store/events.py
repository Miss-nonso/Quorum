"""The append-only run event log and the task-ready wake-up channel."""

from __future__ import annotations

from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncConnection

# Workers LISTEN here. A notification is only a hint to poll now rather than
# at the next interval; correctness never depends on one arriving.
TASK_READY_CHANNEL = "conductor_task_ready"


class EventType(StrEnum):
    """Every event the engine records. The dashboard narrates these."""

    RUN_CREATED = "run_created"
    RUN_SCHEDULED = "run_scheduled"
    RUN_SUCCEEDED = "run_succeeded"
    RUN_FAILED = "run_failed"
    RUN_RESUMED = "run_resumed"
    RUN_CANCELLED = "run_cancelled"
    TASK_ENQUEUED = "task_enqueued"
    TASK_READY = "task_ready"
    TASK_FAILED = "task_failed"
    TASK_CANCELLED = "task_cancelled"
    TASK_CLAIMED = "task_claimed"
    TASK_SUCCEEDED = "task_succeeded"
    TASK_RETRY_SCHEDULED = "task_retry_scheduled"
    TASK_DEAD_LETTERED = "task_dead_lettered"
    TASK_RELEASED = "task_released"
    TASK_REPLAYED = "task_replayed"
    LEASE_EXPIRED = "lease_expired"
    LEASE_LOST = "lease_lost"
    STALE_WRITE_REJECTED = "stale_write_rejected"


_INSERT_EVENT = text(
    "INSERT INTO run_events (run_id, task_id, type, payload) "
    "VALUES (:run_id, :task_id, :type, :payload)"
).bindparams(bindparam("payload", type_=JSONB))


async def record_event(
    conn: AsyncConnection,
    *,
    run_id: UUID,
    task_id: UUID | None,
    type: EventType,
    payload: dict[str, Any] | None = None,
) -> None:
    """Append one event to the run's log."""
    await conn.execute(
        _INSERT_EVENT,
        {"run_id": run_id, "task_id": task_id, "type": type.value, "payload": payload or {}},
    )


async def notify_task_ready(conn: AsyncConnection) -> None:
    """Wake idle workers. Delivered on commit, dropped on rollback."""
    await conn.execute(text("SELECT pg_notify(:channel, '')"), {"channel": TASK_READY_CHANNEL})

"""Small helpers shared by integration and chaos tests."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import asyncpg
from sqlalchemy.ext.asyncio import AsyncEngine

from quorum.domain.backoff import BackoffPolicy
from quorum.store import tasks as task_store
from quorum.tasks import builtin_registry

REGISTRY = builtin_registry()
FAST_RETRY = BackoffPolicy(base_s=0.01, factor=1, max_s=0.01, jitter="none")


async def enqueue(
    engine: AsyncEngine,
    task_type: str = "sleep",
    params: dict[str, Any] | None = None,
    **options: Any,
) -> task_store.EnqueuedTask:
    """Validate and enqueue one standalone job."""
    spec = REGISTRY.build_spec(task_type, params or {"seconds": 0}, **options)
    async with engine.begin() as conn:
        return await task_store.enqueue_job(conn, spec)


async def expire_lease(db: asyncpg.Connection, task_id: UUID) -> None:
    """Make a running task's lease look lapsed, as if its worker went silent."""
    await db.execute(
        "UPDATE task_instances SET lease_expires_at = now() - interval '1 second' WHERE id = $1",
        task_id,
    )


async def task_row(db: asyncpg.Connection, task_id: UUID) -> asyncpg.Record:
    row = await db.fetchrow("SELECT * FROM task_instances WHERE id = $1", task_id)
    assert row is not None
    return row


async def event_types(db: asyncpg.Connection, task_id: UUID) -> list[str]:
    rows = await db.fetch("SELECT type FROM run_events WHERE task_id = $1 ORDER BY id", task_id)
    return [r["type"] for r in rows]

"""Worker registry and liveness."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


async def register_worker(
    conn: AsyncConnection,
    *,
    worker_id: UUID,
    hostname: str,
    pid: int,
    concurrency: int,
    capabilities: list[str],
) -> None:
    """Announce a worker process. Each process start uses a fresh id."""
    await conn.execute(
        text(
            """
            INSERT INTO workers (id, hostname, pid, concurrency, capabilities, status)
            VALUES (:id, :hostname, :pid, :concurrency, :capabilities, 'active')
            """
        ),
        {
            "id": worker_id,
            "hostname": hostname,
            "pid": pid,
            "concurrency": concurrency,
            "capabilities": capabilities,
        },
    )


async def heartbeat(conn: AsyncConnection, *, worker_id: UUID, current_load: int) -> None:
    """Record that the worker is alive. Revives a worker marked unhealthy."""
    await conn.execute(
        text(
            """
            UPDATE workers
            SET last_heartbeat_at = now(), current_load = :load, status = 'active'
            WHERE id = :id AND status <> 'stopped'
            """
        ),
        {"id": worker_id, "load": current_load},
    )


async def mark_stopped(conn: AsyncConnection, *, worker_id: UUID) -> None:
    """Record a clean shutdown."""
    await conn.execute(
        text("UPDATE workers SET status = 'stopped', current_load = 0 WHERE id = :id"),
        {"id": worker_id},
    )


@dataclass(frozen=True, slots=True)
class WorkerStatusChange:
    worker_id: UUID
    status: str


async def mark_stale_workers(
    conn: AsyncConnection, *, unhealthy_after_s: float, dead_after_s: float
) -> list[WorkerStatusChange]:
    """Flag workers whose heartbeats have lapsed.

    This is informational (dashboard, metrics). Task recovery does not depend
    on it: the reaper works from lease expiry alone.
    """
    result = await conn.execute(
        text(
            """
            WITH stale AS (
                SELECT id,
                       CASE WHEN last_heartbeat_at < now() - make_interval(secs => :dead_s)
                            THEN 'dead' ELSE 'unhealthy' END AS new_status
                FROM workers
                WHERE status IN ('active', 'unhealthy')
                  AND last_heartbeat_at < now() - make_interval(secs => :unhealthy_s)
                FOR UPDATE SKIP LOCKED
            )
            UPDATE workers w
            SET status = stale.new_status
            FROM stale
            WHERE w.id = stale.id AND w.status <> stale.new_status
            RETURNING w.id, w.status
            """
        ),
        {"unhealthy_s": unhealthy_after_s, "dead_s": dead_after_s},
    )
    return [WorkerStatusChange(row.id, row.status) for row in result]

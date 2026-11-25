"""Read models for the API and dashboard. Writes live in the other store modules."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

_RUN_COLUMNS = """
    r.id, r.workflow_id, w.name AS workflow_name, r.version, r.status, r.trigger,
    r.scheduled_for, r.started_at, r.finished_at, r.created_at,
    (SELECT count(*) FROM task_instances t WHERE t.run_id = r.id) AS task_count
"""


async def list_runs(
    conn: AsyncConnection,
    *,
    status: str | None = None,
    workflow_id: UUID | None = None,
    trigger: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    where = []
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if status:
        where.append("r.status = :status")
        params["status"] = status
    if workflow_id:
        where.append("r.workflow_id = :workflow_id")
        params["workflow_id"] = workflow_id
    if trigger:
        where.append("r.trigger = :trigger")
        params["trigger"] = trigger
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    # `clause` is assembled only from the fixed fragments above; values are bound.
    rows = await conn.execute(
        text(
            f"SELECT {_RUN_COLUMNS} FROM workflow_runs r "  # noqa: S608
            f"LEFT JOIN workflows w ON w.id = r.workflow_id {clause} "
            "ORDER BY r.created_at DESC, r.id LIMIT :limit OFFSET :offset"
        ),
        params,
    )
    total = (
        await conn.execute(
            text(f"SELECT count(*) FROM workflow_runs r {clause}"),  # noqa: S608
            params,
        )
    ).scalar_one()
    return [dict(r._mapping) for r in rows], int(total)


async def get_run(conn: AsyncConnection, run_id: UUID) -> dict[str, Any] | None:
    row = (
        await conn.execute(
            text(
                f"SELECT {_RUN_COLUMNS} FROM workflow_runs r "  # noqa: S608
                "LEFT JOIN workflows w ON w.id = r.workflow_id WHERE r.id = :id"
            ),
            {"id": run_id},
        )
    ).one_or_none()
    return None if row is None else dict(row._mapping)


async def run_tasks(conn: AsyncConnection, run_id: UUID) -> list[dict[str, Any]]:
    rows = await conn.execute(
        text(
            """
            SELECT t.id, t.node_key, t.task_type, t.params, t.status, t.priority,
                   t.effective_priority, t.attempt, t.max_attempts, t.failure_policy,
                   t.timeout_s, t.not_before, t.lease_owner, t.lease_expires_at,
                   t.fencing_token, t.idempotency_key, t.result, t.error,
                   t.created_at, t.updated_at,
                   COALESCE(
                       (SELECT array_agg(p.node_key ORDER BY p.node_key)
                        FROM task_dependencies d
                        JOIN task_instances p ON p.id = d.depends_on_task_id
                        WHERE d.task_id = t.id),
                       '{}'
                   ) AS depends_on
            FROM task_instances t
            WHERE t.run_id = :id
            ORDER BY t.created_at, t.node_key
            """
        ),
        {"id": run_id},
    )
    return [dict(r._mapping) for r in rows]


async def events(
    conn: AsyncConnection,
    *,
    after_id: int = 0,
    run_id: UUID | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Events in id order, with the task's node key, for timelines and the live feed."""
    rows = await conn.execute(
        text(
            """
            SELECT e.id, e.run_id, e.task_id, t.node_key, e.type, e.payload, e.at
            FROM run_events e LEFT JOIN task_instances t ON t.id = e.task_id
            WHERE e.id > :after AND (CAST(:run_id AS uuid) IS NULL OR e.run_id = :run_id)
            ORDER BY e.id
            LIMIT :limit
            """
        ),
        {"after": after_id, "run_id": run_id, "limit": limit},
    )
    return [dict(r._mapping) for r in rows]


async def nth_latest_event_id(conn: AsyncConnection, n: int, *, run_id: UUID | None = None) -> int:
    """An id such that ``events(after_id=...)`` returns the latest ``n`` events."""
    value = (
        await conn.execute(
            text(
                """
                SELECT COALESCE(min(id) - 1, 0) FROM (
                    SELECT id FROM run_events
                    WHERE CAST(:run_id AS uuid) IS NULL OR run_id = :run_id
                    ORDER BY id DESC LIMIT :n
                ) latest
                """
            ),
            {"n": n, "run_id": run_id},
        )
    ).scalar_one()
    return int(value)


async def latest_event_id(conn: AsyncConnection) -> int:
    value = (await conn.execute(text("SELECT COALESCE(max(id), 0) FROM run_events"))).scalar_one()
    return int(value)


async def list_workers(
    conn: AsyncConnection, *, include_stopped: bool = False
) -> list[dict[str, Any]]:
    rows = await conn.execute(
        text(
            """
            SELECT w.id, w.hostname, w.pid, w.status, w.concurrency, w.current_load,
                   w.capabilities, w.started_at, w.last_heartbeat_at,
                   -- float8: EXTRACT returns numeric, which would reach JSON as a string.
                   EXTRACT(EPOCH FROM now() - w.last_heartbeat_at)::float8 AS heartbeat_age_s,
                   COALESCE(
                       (SELECT json_agg(json_build_object(
                                   'task_id', t.id, 'run_id', t.run_id,
                                   'node_key', t.node_key, 'task_type', t.task_type,
                                   'attempt', t.attempt) ORDER BY t.updated_at)
                        FROM task_instances t
                        WHERE t.status = 'running' AND t.lease_owner = w.id),
                       '[]'
                   ) AS current_tasks
            FROM workers w
            WHERE CAST(:all AS boolean) OR w.status <> 'stopped'
            ORDER BY w.started_at DESC
            LIMIT 200
            """
        ),
        {"all": include_stopped},
    )
    return [dict(r._mapping) for r in rows]


async def list_dead_letters(
    conn: AsyncConnection, *, include_replayed: bool = False, limit: int = 20, offset: int = 0
) -> tuple[list[dict[str, Any]], int]:
    filter_sql = "TRUE" if include_replayed else "d.replayed_at IS NULL"
    rows = await conn.execute(
        text(
            f"""
            SELECT d.id, d.task_id, d.reason, d.payload, d.created_at, d.replayed_at,
                   t.run_id, t.node_key, t.task_type, t.attempt, t.status AS task_status,
                   w.name AS workflow_name
            FROM dead_letters d
            JOIN task_instances t ON t.id = d.task_id
            JOIN workflow_runs r ON r.id = t.run_id
            LEFT JOIN workflows w ON w.id = r.workflow_id
            WHERE {filter_sql}
            ORDER BY d.created_at DESC
            LIMIT :limit OFFSET :offset
            """  # noqa: S608 - filter_sql is one of two constants
        ),
        {"limit": limit, "offset": offset},
    )
    total = (
        await conn.execute(text(f"SELECT count(*) FROM dead_letters d WHERE {filter_sql}"))  # noqa: S608
    ).scalar_one()
    return [dict(r._mapping) for r in rows], int(total)


async def stats(
    conn: AsyncConnection, *, window_s: int = 300, bucket_s: int = 10
) -> dict[str, Any]:
    """Overview counters plus a throughput series of completions per bucket."""
    counts = {
        r.status: int(r.n)
        for r in await conn.execute(
            text("SELECT status, count(*) AS n FROM task_instances GROUP BY status")
        )
    }
    workers = {
        r.status: int(r.n)
        for r in await conn.execute(
            text("SELECT status, count(*) AS n FROM workers GROUP BY status")
        )
    }
    series = await conn.execute(
        text(
            """
            WITH buckets AS (
                SELECT generate_series(
                    to_timestamp(floor(EXTRACT(EPOCH FROM now()) / :bucket) * :bucket)
                        - make_interval(secs => :window),
                    now(),
                    make_interval(secs => :bucket)) AS start
            )
            SELECT b.start,
                   count(e.id) FILTER (WHERE e.type = 'task_succeeded') AS succeeded,
                   count(e.id) FILTER (WHERE e.type IN ('task_dead_lettered', 'task_failed'))
                       AS failed
            FROM buckets b
            LEFT JOIN run_events e
                   ON e.at >= b.start AND e.at < b.start + make_interval(secs => :bucket)
                  AND e.type IN ('task_succeeded', 'task_dead_lettered', 'task_failed')
            GROUP BY b.start ORDER BY b.start
            """
        ),
        {"window": window_s, "bucket": bucket_s},
    )
    open_dead = (
        await conn.execute(text("SELECT count(*) FROM dead_letters WHERE replayed_at IS NULL"))
    ).scalar_one()
    leader = (
        await conn.execute(
            text(
                "SELECT leader_id, hostname, pid, elected_at, heartbeat_at, "
                "heartbeat_at > now() - interval '10 seconds' AS healthy FROM scheduler_state"
            )
        )
    ).one_or_none()
    return {
        "tasks": {
            "pending": counts.get("pending", 0),
            "queued": counts.get("ready", 0),
            "running": counts.get("running", 0),
            "succeeded": counts.get("succeeded", 0),
            "failed": counts.get("failed", 0),
            "dead": counts.get("dead", 0),
            "cancelled": counts.get("cancelled", 0),
        },
        "workers": workers,
        "open_dead_letters": int(open_dead),
        "throughput": [
            {"start": r.start, "succeeded": int(r.succeeded), "failed": int(r.failed)}
            for r in series
        ],
        "bucket_s": bucket_s,
        "scheduler": None if leader is None else dict(leader._mapping),
    }


async def metric_samples(conn: AsyncConnection, *, window_s: int = 300) -> dict[str, Any]:
    """Raw numbers for the Prometheus exposition."""
    by_status = {
        r.status: int(r.n)
        for r in await conn.execute(
            text("SELECT status, count(*) AS n FROM task_instances GROUP BY status")
        )
    }
    workers = {
        r.status: int(r.n)
        for r in await conn.execute(
            text("SELECT status, count(*) AS n FROM workers GROUP BY status")
        )
    }
    event_totals = {
        r.type: int(r.n)
        for r in await conn.execute(
            text("SELECT type, count(*) AS n FROM run_events GROUP BY type")
        )
    }
    # Durations over the recent window: claim -> success (execution), and
    # ready -> claim (queue wait), matched by task and attempt order.
    durations = (
        await conn.execute(
            text(
                """
                WITH done AS (
                    SELECT task_id, at FROM run_events
                    WHERE type = 'task_succeeded' AND at > now() - make_interval(secs => :w)
                ),
                spans AS (
                    SELECT EXTRACT(EPOCH FROM d.at - (
                        SELECT max(c.at) FROM run_events c
                        WHERE c.task_id = d.task_id AND c.type = 'task_claimed' AND c.at <= d.at
                    )) AS seconds
                    FROM done d
                )
                SELECT
                    percentile_cont(0.5) WITHIN GROUP (ORDER BY seconds) AS p50,
                    percentile_cont(0.95) WITHIN GROUP (ORDER BY seconds) AS p95,
                    percentile_cont(0.99) WITHIN GROUP (ORDER BY seconds) AS p99,
                    count(*) AS n, COALESCE(sum(seconds), 0) AS total
                FROM spans
                """
            ),
            {"w": window_s},
        )
    ).one()
    waits = (
        await conn.execute(
            text(
                """
                WITH claims AS (
                    SELECT task_id, at FROM run_events
                    WHERE type = 'task_claimed' AND at > now() - make_interval(secs => :w)
                ),
                spans AS (
                    SELECT EXTRACT(EPOCH FROM c.at - (
                        SELECT max(r.at) FROM run_events r
                        WHERE r.task_id = c.task_id AND r.at < c.at
                          AND r.type IN ('task_enqueued', 'task_ready', 'task_retry_scheduled',
                                         'task_released', 'task_replayed')
                    )) AS seconds
                    FROM claims c
                )
                SELECT
                    percentile_cont(0.5) WITHIN GROUP (ORDER BY seconds) AS p50,
                    percentile_cont(0.95) WITHIN GROUP (ORDER BY seconds) AS p95,
                    percentile_cont(0.99) WITHIN GROUP (ORDER BY seconds) AS p99,
                    count(seconds) AS n, COALESCE(sum(seconds), 0) AS total
                FROM spans
                """
            ),
            {"w": window_s},
        )
    ).one()
    oldest_ready: datetime | None = (
        await conn.execute(
            text(
                "SELECT min(not_before) FROM task_instances "
                "WHERE status = 'ready' AND not_before <= now()"
            )
        )
    ).scalar_one()
    now: datetime = (await conn.execute(text("SELECT now()"))).scalar_one()
    return {
        "tasks_by_status": by_status,
        "workers_by_status": workers,
        "event_totals": event_totals,
        "duration": dict(durations._mapping),
        "queue_wait": dict(waits._mapping),
        "oldest_ready_age_s": (now - oldest_ready).total_seconds() if oldest_ready else 0.0,
    }

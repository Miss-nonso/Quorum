"""Cron schedules: keep them in sync with workflows, and fire the due ones."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from quorum.domain.cron import load_zone, next_fire, parse_cron
from quorum.store import runs as run_store
from quorum.store.events import EventType, record_event


async def db_now(conn: AsyncConnection) -> datetime:
    """The database clock. Schedules are compared with ``now()``, so compute from it too."""
    value: datetime = (await conn.execute(text("SELECT now()"))).scalar_one()
    return value


async def sync_schedule(
    conn: AsyncConnection, *, workflow_id: UUID, cron: str | None, timezone: str, enabled: bool
) -> datetime | None:
    """Create, update or remove a workflow's schedule; return its next fire time.

    Raises:
        quorum.domain.cron.CronError: invalid expression or zone, or it never fires.
    """
    if not cron or not enabled:
        await conn.execute(
            text("DELETE FROM schedules WHERE workflow_id = :id"), {"id": workflow_id}
        )
        return None
    upcoming = next_fire(parse_cron(cron), load_zone(timezone), await db_now(conn))
    await conn.execute(
        text(
            """
            INSERT INTO schedules (workflow_id, cron, timezone, next_fire_at)
            VALUES (:id, :cron, :tz, :next)
            ON CONFLICT (workflow_id) DO UPDATE
            SET cron = EXCLUDED.cron, timezone = EXCLUDED.timezone,
                next_fire_at = EXCLUDED.next_fire_at
            """
        ),
        {"id": workflow_id, "cron": cron, "tz": timezone, "next": upcoming},
    )
    return upcoming


@dataclass(frozen=True, slots=True)
class Firing:
    workflow_id: UUID
    scheduled_for: datetime
    run_id: UUID | None  # None if a run for this instant already existed
    missed: int  # due instants skipped because the scheduler was down
    next_fire_at: datetime


async def fire_due_schedules(conn: AsyncConnection, *, limit: int = 50) -> list[Firing]:
    """Start a run for every schedule whose time has come.

    **Misfire policy:** if the scheduler was down across several fire times,
    the schedule fires *once* (for the earliest missed instant) and then
    skips ahead to the first time after now. Replaying a backlog of
    identical runs is rarely what anyone wants; the skip count is recorded.

    Safe under concurrent schedulers: rows are claimed with SKIP LOCKED, and
    the unique (workflow_id, scheduled_for) index makes a duplicate run for
    the same instant impossible.
    """
    now = await db_now(conn)
    due = (
        await conn.execute(
            text(
                """
                SELECT id, workflow_id, cron, timezone, next_fire_at FROM schedules
                WHERE next_fire_at <= now()
                ORDER BY next_fire_at
                LIMIT :limit
                FOR UPDATE SKIP LOCKED
                """
            ),
            {"limit": limit},
        )
    ).all()

    firings: list[Firing] = []
    for row in due:
        schedule, zone = parse_cron(row.cron), load_zone(row.timezone)
        missed, upcoming = 0, next_fire(schedule, zone, row.next_fire_at)
        while upcoming <= now:
            missed += 1
            upcoming = next_fire(schedule, zone, upcoming)

        run_id: UUID | None = None
        try:
            async with conn.begin_nested():
                created = await run_store.create_workflow_run(
                    conn, row.workflow_id, trigger="schedule", scheduled_for=row.next_fire_at
                )
                run_id = created.run_id
                await record_event(
                    conn,
                    run_id=run_id,
                    task_id=None,
                    type=EventType.RUN_SCHEDULED,
                    payload={
                        "cron": row.cron,
                        "timezone": row.timezone,
                        "scheduled_for": row.next_fire_at.isoformat(),
                        "missed": missed,
                    },
                )
        except IntegrityError:
            pass  # another scheduler already fired this instant
        await conn.execute(
            text("UPDATE schedules SET next_fire_at = :next, last_fired_at = now() WHERE id = :id"),
            {"id": row.id, "next": upcoming},
        )
        firings.append(Firing(row.workflow_id, row.next_fire_at, run_id, missed, upcoming))
    return firings

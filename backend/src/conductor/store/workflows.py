"""Workflow definitions: create, update (new version), read, list."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from conductor.domain.dag import WorkflowDefinition
from conductor.store.schedules import sync_schedule


class WorkflowNotFoundError(LookupError):
    pass


class WorkflowNameTakenError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class WorkflowRecord:
    id: UUID
    name: str
    version: int
    definition: WorkflowDefinition
    cron: str | None
    timezone: str
    enabled: bool
    created_at: datetime


_COLUMNS = "id, name, version, definition, cron, timezone, enabled, created_at"


def _record(row: Any) -> WorkflowRecord:
    return WorkflowRecord(
        id=row.id,
        name=row.name,
        version=row.version,
        definition=WorkflowDefinition.model_validate(row.definition),
        cron=row.cron,
        timezone=row.timezone,
        enabled=row.enabled,
        created_at=row.created_at,
    )


async def create_workflow(
    conn: AsyncConnection,
    *,
    name: str,
    definition: WorkflowDefinition,
    cron: str | None = None,
    timezone: str = "UTC",
    enabled: bool = True,
) -> WorkflowRecord:
    """Store an already-validated definition as version 1, and its schedule if any.

    Raises:
        WorkflowNameTakenError: a workflow with this name exists.
        conductor.domain.cron.CronError: the cron expression or time zone is invalid.
    """
    try:
        async with conn.begin_nested():
            row = (
                await conn.execute(
                    text(
                        f"""
                        INSERT INTO workflows (name, definition, cron, timezone, enabled)
                        VALUES (:name, :definition, :cron, :timezone, :enabled)
                        RETURNING {_COLUMNS}
                        """  # noqa: S608 - _COLUMNS is a constant
                    ).bindparams(bindparam("definition", type_=JSONB)),
                    {
                        "name": name,
                        "definition": definition.model_dump(mode="json"),
                        "cron": cron,
                        "timezone": timezone,
                        "enabled": enabled,
                    },
                )
            ).one()
    except IntegrityError as exc:
        raise WorkflowNameTakenError(f"a workflow named {name!r} already exists") from exc
    await sync_schedule(conn, workflow_id=row.id, cron=cron, timezone=timezone, enabled=enabled)
    return _record(row)


async def update_workflow(
    conn: AsyncConnection,
    workflow_id: UUID,
    *,
    definition: WorkflowDefinition,
    cron: str | None,
    timezone: str,
    enabled: bool,
) -> WorkflowRecord:
    """Replace the definition and bump the version.

    Runs already materialised keep the tasks of the version they started
    with; only new runs see the change.
    """
    row = (
        await conn.execute(
            text(
                f"""
                UPDATE workflows
                SET definition = :definition, cron = :cron, timezone = :timezone,
                    enabled = :enabled, version = version + 1
                WHERE id = :id
                RETURNING {_COLUMNS}
                """  # noqa: S608
            ).bindparams(bindparam("definition", type_=JSONB)),
            {
                "id": workflow_id,
                "definition": definition.model_dump(mode="json"),
                "cron": cron,
                "timezone": timezone,
                "enabled": enabled,
            },
        )
    ).one_or_none()
    if row is None:
        raise WorkflowNotFoundError(f"no workflow {workflow_id}")
    await sync_schedule(conn, workflow_id=row.id, cron=cron, timezone=timezone, enabled=enabled)
    return _record(row)


async def get_workflow(conn: AsyncConnection, workflow_id: UUID) -> WorkflowRecord:
    row = (
        await conn.execute(
            text(f"SELECT {_COLUMNS} FROM workflows WHERE id = :id"),  # noqa: S608
            {"id": workflow_id},
        )
    ).one_or_none()
    if row is None:
        raise WorkflowNotFoundError(f"no workflow {workflow_id}")
    return _record(row)


async def get_workflow_by_name(conn: AsyncConnection, name: str) -> WorkflowRecord | None:
    row = (
        await conn.execute(
            text(f"SELECT {_COLUMNS} FROM workflows WHERE name = :name"),  # noqa: S608
            {"name": name},
        )
    ).one_or_none()
    return None if row is None else _record(row)


async def list_workflows(
    conn: AsyncConnection, *, limit: int = 50, offset: int = 0
) -> tuple[list[WorkflowRecord], int]:
    rows = await conn.execute(
        text(
            f"SELECT {_COLUMNS} FROM workflows ORDER BY name LIMIT :limit OFFSET :offset"  # noqa: S608
        ),
        {"limit": limit, "offset": offset},
    )
    total = (await conn.execute(text("SELECT count(*) FROM workflows"))).scalar_one()
    return [_record(r) for r in rows], int(total)

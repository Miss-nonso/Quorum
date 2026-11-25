"""REST resources: workflows, runs, jobs, workers, dead letters, stats, task types."""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Query, status
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from conductor.api.deps import Limit, Offset, ReadConn, Registry, WriteConn
from conductor.api.errors import ApiError, ErrorResponse
from conductor.api.schemas import (
    DeadLetterOut,
    EventOut,
    JobIn,
    JobOut,
    Page,
    ReplayOut,
    RunDetail,
    RunOut,
    StatsOut,
    TaskOut,
    TaskTypeOut,
    WorkerOut,
    WorkflowIn,
    WorkflowOut,
    WorkflowUpdate,
)
from conductor.domain.cron import CronError
from conductor.domain.dag import DagError
from conductor.store import queries
from conductor.store import runs as run_store
from conductor.store import tasks as task_store
from conductor.store import workflows as wf_store
from conductor.store.workflows import WorkflowRecord
from conductor.worker.registry import InvalidParamsError, UnknownTaskTypeError

router = APIRouter()

Responses = dict[int | str, dict[str, Any]]
NOT_FOUND: Responses = {404: {"model": ErrorResponse, "description": "Not found."}}
CONFLICT: Responses = {
    409: {"model": ErrorResponse, "description": "Conflicts with current state."}
}
INVALID: Responses = {422: {"model": ErrorResponse, "description": "Invalid input."}}


def _not_found(kind: str, ident: object) -> ApiError:
    return ApiError(404, "not_found", f"No {kind} with id {ident}.")


async def _workflow_out(conn: Any, record: WorkflowRecord) -> WorkflowOut:
    next_fire = (
        await conn.execute(
            text("SELECT next_fire_at FROM schedules WHERE workflow_id = :id"), {"id": record.id}
        )
    ).scalar_one_or_none()
    return WorkflowOut(
        id=record.id,
        name=record.name,
        version=record.version,
        definition=record.definition,
        cron=record.cron,
        timezone=record.timezone,
        enabled=record.enabled,
        created_at=record.created_at,
        next_fire_at=next_fire,
    )


def _validate_or_422(registry: Registry, body: WorkflowIn | WorkflowUpdate) -> None:
    try:
        registry.validate_workflow(body.definition)
    except DagError as exc:
        raise ApiError(
            422, "invalid_workflow", "The workflow definition is not valid.", exc.problems
        ) from exc


# ------------------------------------------------------------------ workflows


@router.post(
    "/workflows",
    status_code=status.HTTP_201_CREATED,
    response_model=WorkflowOut,
    tags=["workflows"],
    summary="Create a workflow",
    responses={**CONFLICT, **INVALID},
)
async def create_workflow(body: WorkflowIn, conn: WriteConn, registry: Registry) -> WorkflowOut:
    """Validate the DAG (structure, task types, params, references) and the schedule, then save."""
    _validate_or_422(registry, body)
    try:
        record = await wf_store.create_workflow(
            conn,
            name=body.name,
            definition=body.definition,
            cron=body.cron,
            timezone=body.timezone,
            enabled=body.enabled,
        )
    except wf_store.WorkflowNameTakenError as exc:
        raise ApiError(409, "name_taken", str(exc)) from exc
    except CronError as exc:
        raise ApiError(422, "invalid_schedule", str(exc)) from exc
    return await _workflow_out(conn, record)


@router.get(
    "/workflows", response_model=Page[WorkflowOut], tags=["workflows"], summary="List workflows"
)
async def list_workflows(
    conn: ReadConn, limit: Limit = 20, offset: Offset = 0
) -> Page[WorkflowOut]:
    records, total = await wf_store.list_workflows(conn, limit=limit, offset=offset)
    items = [await _workflow_out(conn, r) for r in records]
    return Page(items=items, total=total, limit=limit, offset=offset)


@router.get(
    "/workflows/{workflow_id}",
    response_model=WorkflowOut,
    tags=["workflows"],
    summary="Get a workflow",
    responses=NOT_FOUND,
)
async def get_workflow(workflow_id: UUID, conn: ReadConn) -> WorkflowOut:
    try:
        return await _workflow_out(conn, await wf_store.get_workflow(conn, workflow_id))
    except wf_store.WorkflowNotFoundError as exc:
        raise _not_found("workflow", workflow_id) from exc


@router.put(
    "/workflows/{workflow_id}",
    response_model=WorkflowOut,
    tags=["workflows"],
    summary="Update a workflow (creates a new version)",
    responses={**NOT_FOUND, **INVALID},
)
async def update_workflow(
    workflow_id: UUID, body: WorkflowUpdate, conn: WriteConn, registry: Registry
) -> WorkflowOut:
    """Runs already started keep the version they began with."""
    _validate_or_422(registry, body)
    try:
        record = await wf_store.update_workflow(
            conn,
            workflow_id,
            definition=body.definition,
            cron=body.cron,
            timezone=body.timezone,
            enabled=body.enabled,
        )
    except wf_store.WorkflowNotFoundError as exc:
        raise _not_found("workflow", workflow_id) from exc
    except CronError as exc:
        raise ApiError(422, "invalid_schedule", str(exc)) from exc
    return await _workflow_out(conn, record)


@router.post(
    "/workflows/{workflow_id}/runs",
    status_code=status.HTTP_201_CREATED,
    response_model=RunOut,
    tags=["runs"],
    summary="Trigger a run",
    responses=NOT_FOUND,
)
async def trigger_run(workflow_id: UUID, conn: WriteConn) -> RunOut:
    try:
        created = await run_store.create_workflow_run(conn, workflow_id, trigger="manual")
    except run_store.RunNotFoundError as exc:
        raise _not_found("workflow", workflow_id) from exc
    row = await queries.get_run(conn, created.run_id)
    return RunOut.model_validate(row)


# ------------------------------------------------------------------ runs


@router.get("/runs", response_model=Page[RunOut], tags=["runs"], summary="List runs (history)")
async def list_runs(
    conn: ReadConn,
    limit: Limit = 20,
    offset: Offset = 0,
    status_filter: Annotated[
        str | None,
        Query(alias="status", pattern="^(pending|running|succeeded|failed|cancelled)$"),
    ] = None,
    workflow_id: UUID | None = None,
    trigger: Annotated[str | None, Query(pattern="^(manual|schedule|api|demo)$")] = None,
) -> Page[RunOut]:
    rows, total = await queries.list_runs(
        conn,
        status=status_filter,
        workflow_id=workflow_id,
        trigger=trigger,
        limit=limit,
        offset=offset,
    )
    return Page(
        items=[RunOut.model_validate(r) for r in rows], total=total, limit=limit, offset=offset
    )


@router.get(
    "/runs/{run_id}",
    response_model=RunDetail,
    tags=["runs"],
    summary="Get a run with its tasks and events",
    responses=NOT_FOUND,
)
async def get_run(run_id: UUID, conn: ReadConn) -> RunDetail:
    row = await queries.get_run(conn, run_id)
    if row is None:
        raise _not_found("run", run_id)
    tasks = await queries.run_tasks(conn, run_id)
    events = await queries.events(conn, run_id=run_id, limit=2000)
    return RunDetail(
        **RunOut.model_validate(row).model_dump(),
        tasks=[TaskOut.model_validate(t) for t in tasks],
        events=[EventOut.model_validate(e) for e in events],
    )


@router.post(
    "/runs/{run_id}/cancel",
    response_model=RunOut,
    tags=["runs"],
    summary="Cancel a run",
    responses={**NOT_FOUND, **CONFLICT},
)
async def cancel_run(run_id: UUID, conn: WriteConn) -> RunOut:
    """Cancels unfinished tasks; a running task's worker is fenced off and stops."""
    try:
        await run_store.cancel_run(conn, run_id)
    except run_store.RunNotFoundError as exc:
        raise _not_found("run", run_id) from exc
    except run_store.RunNotCancellableError as exc:
        raise ApiError(409, "not_cancellable", str(exc)) from exc
    return RunOut.model_validate(await queries.get_run(conn, run_id))


# ------------------------------------------------------------------ jobs


@router.post(
    "/jobs",
    status_code=status.HTTP_201_CREATED,
    response_model=JobOut,
    tags=["jobs"],
    summary="Enqueue a standalone job",
    responses={**INVALID, **CONFLICT},
)
async def enqueue_job(body: JobIn, conn: WriteConn, registry: Registry) -> JobOut:
    try:
        spec = registry.build_spec(
            body.task_type,
            body.params,
            priority=body.priority,
            max_attempts=body.max_attempts,
            backoff=body.backoff,
            timeout_s=body.timeout_s,
            idempotency_key=body.idempotency_key,
            delay_s=body.delay_s,
        )
    except UnknownTaskTypeError as exc:
        raise ApiError(422, "unknown_task_type", str(exc)) from exc
    except InvalidParamsError as exc:
        raise ApiError(422, "invalid_params", str(exc).split(":")[0], exc.errors) from exc
    except ValueError as exc:
        raise ApiError(422, "invalid_job", str(exc)) from exc
    try:
        async with conn.begin_nested():
            job = await task_store.enqueue_job(conn, spec)
    except IntegrityError as exc:  # the unique index is the authority, not a pre-check
        raise ApiError(
            409, "duplicate_idempotency_key", "A task with this idempotency key already exists."
        ) from exc
    return JobOut(run_id=job.run_id, task_id=job.task_id, idempotency_key=job.idempotency_key)


# ------------------------------------------------------------------ workers & DLQ


@router.get("/workers", response_model=list[WorkerOut], tags=["workers"], summary="List workers")
async def list_workers(conn: ReadConn, include_stopped: bool = False) -> list[WorkerOut]:
    rows = await queries.list_workers(conn, include_stopped=include_stopped)
    return [WorkerOut.model_validate(r) for r in rows]


@router.get(
    "/dead-letters",
    response_model=Page[DeadLetterOut],
    tags=["dead letters"],
    summary="List dead-lettered tasks",
)
async def list_dead_letters(
    conn: ReadConn, limit: Limit = 20, offset: Offset = 0, include_replayed: bool = False
) -> Page[DeadLetterOut]:
    rows, total = await queries.list_dead_letters(
        conn, include_replayed=include_replayed, limit=limit, offset=offset
    )
    return Page(
        items=[DeadLetterOut.model_validate(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/dead-letters/{dead_letter_id}/replay",
    response_model=ReplayOut,
    tags=["dead letters"],
    summary="Replay a dead-lettered task",
    responses={**NOT_FOUND, **CONFLICT},
)
async def replay(dead_letter_id: UUID, conn: WriteConn) -> ReplayOut:
    """Requeue with a fresh attempt budget. The idempotency key is kept, so effects don't repeat."""
    try:
        task_id = await task_store.replay_dead_letter(conn, dead_letter_id)
    except task_store.DeadLetterNotFoundError as exc:
        raise ApiError(404, "not_found", f"No open dead letter with id {dead_letter_id}.") from exc
    except run_store.RunNotCancellableError as exc:
        raise ApiError(409, "run_cancelled", str(exc)) from exc
    run_id = await run_store.run_of_task(conn, task_id)
    assert run_id is not None  # noqa: S101 - the task was just updated in this transaction
    return ReplayOut(task_id=task_id, run_id=run_id)


# ------------------------------------------------------------------ overview


@router.get("/stats", response_model=StatsOut, tags=["overview"], summary="Overview counters")
async def stats(conn: ReadConn) -> StatsOut:
    return StatsOut.model_validate(await queries.stats(conn))


@router.get(
    "/task-types",
    response_model=list[TaskTypeOut],
    tags=["overview"],
    summary="Built-in task types and their param schemas",
)
async def task_types(registry: Registry) -> list[TaskTypeOut]:
    out = []
    for name in registry.names():
        t = registry.get(name)
        out.append(
            TaskTypeOut(
                name=t.name,
                description=t.description,
                has_side_effects=t.has_side_effects,
                params_schema=t.params_model.model_json_schema(),
            )
        )
    return out

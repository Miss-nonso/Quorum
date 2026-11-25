"""Workflow runs end to end: materialisation, data flow, policies, replay, races."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any

import asyncpg
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from conductor.domain.dag import WorkflowDefinition
from conductor.samples import sample_definition
from conductor.store import runs as run_store
from conductor.store import tasks as task_store
from conductor.store import workflows as wf_store
from conductor.worker.runtime import Worker, WorkerConfig
from tests.conftest import eventually
from tests.integration.helpers import REGISTRY
from tests.integration.http_fixture import local_http_server

FAST = WorkerConfig(
    concurrency=4, lease_ttl_s=2.0, heartbeat_interval_s=0.1, poll_interval_s=0.05, listen=False
)
FAST_BACKOFF = {"base_s": 0.01, "factor": 1, "max_s": 0.01, "jitter": "none"}


@pytest.fixture
def base_url() -> Iterator[str]:
    with local_http_server() as url:
        yield url


@asynccontextmanager
async def worker(engine: AsyncEngine, database_url: str) -> AsyncIterator[Worker]:
    w = Worker(engine=engine, registry=REGISTRY, config=FAST, database_url=database_url)
    task = asyncio.create_task(w.run())
    try:
        yield w
    finally:
        w.request_stop()
        await asyncio.wait_for(task, timeout=10)


async def save(engine: AsyncEngine, definition: WorkflowDefinition, name: str = "wf") -> uuid.UUID:
    REGISTRY.validate_workflow(definition)
    async with engine.begin() as conn:
        record = await wf_store.create_workflow(conn, name=name, definition=definition)
    return record.id


async def start(engine: AsyncEngine, workflow_id: uuid.UUID) -> run_store.CreatedRun:
    async with engine.begin() as conn:
        return await run_store.create_workflow_run(conn, workflow_id)


async def run_status(db: asyncpg.Connection, run_id: uuid.UUID) -> str:
    return str(await db.fetchval("SELECT status FROM workflow_runs WHERE id = $1", run_id))


async def statuses(db: asyncpg.Connection, run_id: uuid.UUID) -> dict[str, str]:
    rows = await db.fetch("SELECT node_key, status FROM task_instances WHERE run_id = $1", run_id)
    return {r["node_key"]: r["status"] for r in rows}


async def wait_run(db: asyncpg.Connection, run_id: uuid.UUID, *want: str) -> str:
    async def done() -> str | None:
        status = await run_status(db, run_id)
        return status if status in want else None

    return await eventually(done, within=20, what=f"run to reach {want}")


def nodes(*specs: dict[str, Any]) -> WorkflowDefinition:
    return WorkflowDefinition.model_validate({"nodes": list(specs)})


# ------------------------------------------------------------------ the sample pipeline


async def test_sample_pipeline_runs_end_to_end_with_data_flowing_between_steps(
    engine: AsyncEngine, database_url: str, db: asyncpg.Connection, base_url: str
) -> None:
    run = await start(engine, await save(engine, sample_definition(f"{base_url}/orders")))
    initial = await statuses(db, run.run_id)
    assert initial == {
        "fetch": "ready",
        "validate": "pending",
        "transform": "pending",
        "store": "pending",
        "report": "pending",
        "notify": "pending",
    }

    async with worker(engine, database_url):
        assert await wait_run(db, run.run_id, "succeeded", "failed") == "succeeded"

    stored = await db.fetchrow("SELECT * FROM stored_records")
    assert stored is not None
    records = json.loads(stored["records"])
    # Paid orders with total >= 50, highest first.
    assert [r["order"] for r in records] == ["ord-1005", "ord-1007", "ord-1001", "ord-1004"]
    message = await db.fetchval("SELECT message FROM notifications")
    assert message.startswith("Stored 4 large paid orders; report is ")
    report = json.loads(
        await db.fetchval(
            "SELECT result FROM task_instances WHERE run_id = $1 AND node_key = 'report'",
            run.run_id,
        )
    )
    assert "| ord-1005 | Ken | 720.0 |" in report["content"]
    finished = await db.fetchval(
        "SELECT finished_at IS NOT NULL FROM workflow_runs WHERE id = $1", run.run_id
    )
    assert finished


async def test_parallel_branches_are_released_together(
    engine: AsyncEngine, database_url: str, db: asyncpg.Connection, base_url: str
) -> None:
    run = await start(engine, await save(engine, sample_definition(f"{base_url}/orders")))
    async with worker(engine, database_url):
        await wait_run(db, run.run_id, "succeeded")
    released = await db.fetch(
        """
        SELECT t.node_key FROM run_events e JOIN task_instances t ON t.id = e.task_id
        WHERE e.run_id = $1 AND e.type = 'task_ready'
          AND e.payload->>'unblocked_by' = $2
        """,
        run.run_id,
        str(run.task_ids["transform"]),
    )
    assert sorted(r["node_key"] for r in released) == ["report", "store"]
    # notify needed both branches: it was released by whichever finished second.
    notify_released = await db.fetchval(
        "SELECT count(*) FROM run_events WHERE task_id = $1 AND type = 'task_ready'",
        run.task_ids["notify"],
    )
    assert notify_released == 1


async def test_invalid_upstream_data_dead_letters_validate_and_blocks_the_rest(
    engine: AsyncEngine, database_url: str, db: asyncpg.Connection, base_url: str
) -> None:
    run = await start(engine, await save(engine, sample_definition(f"{base_url}/bad-orders")))
    async with worker(engine, database_url):
        assert await wait_run(db, run.run_id, "failed") == "failed"
    assert await statuses(db, run.run_id) == {
        "fetch": "succeeded",
        "validate": "dead",
        "transform": "pending",
        "store": "pending",
        "report": "pending",
        "notify": "pending",
    }
    reason = await db.fetchval(
        "SELECT error->>'message' FROM task_instances WHERE node_key = 'validate'"
    )
    assert "$[0].total: less than minimum 0" in reason
    assert (
        await db.fetchval("SELECT attempt FROM task_instances WHERE node_key = 'validate'") == 1
    ), "bad data is a permanent failure: no retries"


async def test_replaying_the_dead_task_resumes_the_run(
    engine: AsyncEngine, database_url: str, db: asyncpg.Connection
) -> None:
    definition = nodes(
        {"key": "a", "task_type": "sleep", "params": {"seconds": 0}},
        {
            "key": "b",
            "task_type": "flaky",
            "depends_on": ["a"],
            "params": {"failure_rate": 1},
            "max_attempts": 2,
            "backoff": FAST_BACKOFF,
        },
        {"key": "c", "task_type": "sleep", "depends_on": ["b"], "params": {"seconds": 0}},
    )
    run = await start(engine, await save(engine, definition))
    async with worker(engine, database_url):
        await wait_run(db, run.run_id, "failed")
        assert (await statuses(db, run.run_id))["c"] == "pending"

        # The operator fixes the cause, then replays from the dead-letter queue.
        await db.execute(
            "UPDATE task_instances SET params = '{\"failure_rate\": 0}' WHERE id = $1",
            run.task_ids["b"],
        )
        letter = await db.fetchval(
            "SELECT id FROM dead_letters WHERE task_id = $1", run.task_ids["b"]
        )
        async with engine.begin() as conn:
            await task_store.replay_dead_letter(conn, letter)
        assert await run_status(db, run.run_id) == "running"
        assert await wait_run(db, run.run_id, "succeeded") == "succeeded"

    events = [
        r["type"]
        for r in await db.fetch(
            "SELECT type FROM run_events WHERE run_id = $1 AND task_id IS NULL ORDER BY id",
            run.run_id,
        )
    ]
    assert events == ["run_created", "run_failed", "run_resumed", "run_succeeded"]


async def test_fail_run_policy_cancels_everything_else(
    engine: AsyncEngine, database_url: str, db: asyncpg.Connection
) -> None:
    definition = nodes(
        {
            "key": "bad",
            "task_type": "flaky",
            "params": {"failure_rate": 1},
            "failure_policy": "fail_run",
            "max_attempts": 1,
        },
        {"key": "slow", "task_type": "sleep", "params": {"seconds": 30}},
        {"key": "after", "task_type": "sleep", "depends_on": ["bad"]},
    )
    run = await start(engine, await save(engine, definition))
    async with worker(engine, database_url):
        assert await wait_run(db, run.run_id, "failed") == "failed"
        assert await statuses(db, run.run_id) == {
            "bad": "failed",
            "slow": "cancelled",
            "after": "cancelled",
        }
        # The worker running `slow` notices its lease is gone and stops.
        await eventually(
            lambda: _event(db, run.task_ids["slow"], "lease_lost"), within=5, what="lease_lost"
        )
    assert await db.fetchval("SELECT count(*) FROM dead_letters") == 0


async def test_continue_policy_lets_dependents_run_but_marks_the_run_failed(
    engine: AsyncEngine, database_url: str, db: asyncpg.Connection
) -> None:
    definition = nodes(
        {
            "key": "optional",
            "task_type": "flaky",
            "params": {"failure_rate": 1},
            "failure_policy": "continue",
            "max_attempts": 1,
        },
        {"key": "after", "task_type": "sleep", "depends_on": ["optional"]},
    )
    run = await start(engine, await save(engine, definition))
    async with worker(engine, database_url):
        assert await wait_run(db, run.run_id, "failed") == "failed"
    assert await statuses(db, run.run_id) == {"optional": "failed", "after": "succeeded"}


async def test_cancelling_a_run_fences_off_its_running_task(
    engine: AsyncEngine, database_url: str, db: asyncpg.Connection
) -> None:
    definition = nodes({"key": "slow", "task_type": "sleep", "params": {"seconds": 30}})
    run = await start(engine, await save(engine, definition))
    async with worker(engine, database_url):
        await eventually(
            lambda: _status_is(db, run.task_ids["slow"], "running"), what="task to start"
        )
        async with engine.begin() as conn:
            assert await run_store.cancel_run(conn, run.run_id) == [run.task_ids["slow"]]
        await eventually(
            lambda: _event(db, run.task_ids["slow"], "lease_lost"), within=5, what="lease_lost"
        )
    assert await run_status(db, run.run_id) == "cancelled"
    async with engine.begin() as conn:
        with pytest.raises(run_store.RunNotCancellableError):
            await run_store.cancel_run(conn, run.run_id)


async def test_concurrent_parent_completions_always_release_the_child(
    engine: AsyncEngine, db: asyncpg.Connection
) -> None:
    """Two parents finishing at the same instant must never strand their child.

    Without the run lock, each transaction sees the other parent as still
    running and neither releases the child. Repeated to make the race likely.
    """
    definition = nodes(
        {"key": "left", "task_type": "sleep"},
        {"key": "right", "task_type": "sleep"},
        {"key": "join", "task_type": "sleep", "depends_on": ["left", "right"]},
    )
    workflow_id = await save(engine, definition)
    for iteration in range(25):
        run = await start(engine, workflow_id)
        leases: list[tuple[task_store.ClaimedTask, uuid.UUID]] = []
        for _ in ("left", "right"):
            worker_id = uuid.uuid4()
            async with engine.begin() as conn:
                [claim] = await task_store.claim_tasks(
                    conn, worker_id=worker_id, limit=1, lease_ttl_s=30
                )
            leases.append((claim, worker_id))
        assert {c.node_key for c, _ in leases} == {"left", "right"}

        async def finish(claim: task_store.ClaimedTask, worker_id: uuid.UUID) -> None:
            async with engine.begin() as conn:
                await task_store.complete_task(
                    conn,
                    task_id=claim.id,
                    worker_id=worker_id,
                    fencing_token=claim.fencing_token,
                    result={},
                )

        await asyncio.gather(*(finish(c, w) for c, w in leases))
        assert (await statuses(db, run.run_id))["join"] == "ready", (
            f"child stranded on iteration {iteration}"
        )
        async with engine.begin() as conn:  # clear the board for the next iteration
            await run_store.cancel_run(conn, run.run_id)


async def test_http_fetch_enforces_allowlist_redirects_size_and_retries_5xx(
    engine: AsyncEngine, database_url: str, db: asyncpg.Connection, base_url: str
) -> None:
    definition = nodes(
        {
            "key": "off_list",
            "task_type": "http_fetch",
            "params": {"url": "http://example.com/"},
            "failure_policy": "continue",
            "max_attempts": 1,
        },
        {
            "key": "redirect",
            "task_type": "http_fetch",
            "params": {"url": f"{base_url}/redirect"},
            "failure_policy": "continue",
            "max_attempts": 1,
        },
        {
            "key": "too_big",
            "task_type": "http_fetch",
            "params": {"url": f"{base_url}/big"},
            "failure_policy": "continue",
            "max_attempts": 1,
        },
        {
            "key": "flaky_upstream",
            "task_type": "http_fetch",
            "params": {"url": f"{base_url}/boom"},
            "failure_policy": "continue",
            "max_attempts": 2,
            "backoff": FAST_BACKOFF,
        },
        {"key": "plain_text", "task_type": "http_fetch", "params": {"url": f"{base_url}/text"}},
    )
    run = await start(engine, await save(engine, definition))
    async with worker(engine, database_url):
        await wait_run(db, run.run_id, "failed")
    rows = {
        r["node_key"]: r
        for r in await db.fetch(
            "SELECT node_key, status, attempt, error, result FROM task_instances WHERE run_id = $1",
            run.run_id,
        )
    }
    assert "not on the allowlist" in rows["off_list"]["error"]
    assert "HTTP 302" in rows["redirect"]["error"]
    assert "byte limit" in rows["too_big"]["error"]
    assert rows["flaky_upstream"]["attempt"] == 2, "5xx is retried"
    assert json.loads(rows["plain_text"]["result"])["text"] == "hello"


async def test_workflow_versioning_and_name_uniqueness(engine: AsyncEngine) -> None:
    definition = nodes({"key": "a", "task_type": "sleep"})
    async with engine.begin() as conn:
        created = await wf_store.create_workflow(conn, name="dup", definition=definition)
        with pytest.raises(wf_store.WorkflowNameTakenError):
            await wf_store.create_workflow(conn, name="dup", definition=definition)
        updated = await wf_store.update_workflow(
            conn, created.id, definition=definition, cron=None, timezone="UTC", enabled=False
        )
        assert (updated.version, updated.enabled) == (2, False)
        listed, total = await wf_store.list_workflows(conn)
        assert total == 1
        assert listed[0].id == created.id


# ------------------------------------------------------------------ helpers


async def _event(db: asyncpg.Connection, task_id: uuid.UUID, kind: str) -> bool:
    found = await db.fetchval(
        "SELECT count(*) FROM run_events WHERE task_id = $1 AND type = $2", task_id, kind
    )
    return bool(found)


async def _status_is(db: asyncpg.Connection, task_id: uuid.UUID, status: str) -> bool:
    current = await db.fetchval("SELECT status FROM task_instances WHERE id = $1", task_id)
    return bool(current == status)

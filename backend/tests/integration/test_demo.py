"""The public demo: sandboxes, chaos controls, limits and reset."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator

import asyncpg
import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from conductor.api.app import create_app
from conductor.core.config import Settings
from conductor.demo import service
from conductor.seed import seed
from conductor.worker.runtime import Worker, WorkerConfig
from tests.conftest import eventually, make_client
from tests.integration.helpers import REGISTRY

ME = {"X-Sandbox-Id": "visitor-aaaaaaaaaaaaaaaa"}
OTHER = {"X-Sandbox-Id": "visitor-bbbbbbbbbbbbbbbb"}


@pytest.fixture
async def demo(database_url: str, engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    await seed(engine, dataset_url="http://127.0.0.1:9/unused")
    settings = Settings(
        database_url=database_url,
        demo_mode=True,
        demo_step_delay_s=0,
        demo_max_runs_per_sandbox=2,
        demo_flood_low=8,
        demo_flood_high=2,
        demo_max_queued=25,
        demo_actions_per_ip_per_min=50,
    )
    app = create_app(settings)
    async with make_client(app) as client:
        yield client
    await app.state.engine.dispose()


def code(response: httpx.Response) -> str:
    return str(response.json()["error"]["code"])


# ------------------------------------------------------------------ gating


async def test_demo_endpoints_are_off_unless_demo_mode(client: httpx.AsyncClient) -> None:
    response = await client.post("/api/v1/demo/runs", headers=ME)
    assert (response.status_code, code(response)) == (403, "demo_disabled")
    assert (await client.get("/api/v1/demo/dataset")).status_code == 200, "dataset is always public"


async def test_meta_reports_demo_mode(demo: httpx.AsyncClient, client: httpx.AsyncClient) -> None:
    assert (await demo.get("/api/v1/meta")).json()["demo_mode"] is True
    assert (await client.get("/api/v1/meta")).json()["demo_mode"] is False


async def test_sandbox_ids_are_validated(demo: httpx.AsyncClient) -> None:
    for bad in ("short", "has spaces in it!!!!!", "x" * 65):
        response = await demo.post("/api/v1/demo/runs", headers={"X-Sandbox-Id": bad})
        assert response.status_code == 422
    assert (await demo.post("/api/v1/demo/runs")).status_code == 422, "header required"


async def test_general_writes_are_locked_in_demo_mode(demo: httpx.AsyncClient) -> None:
    for method, path, body in (
        ("POST", "/api/v1/jobs", {"task_type": "sleep"}),
        (
            "POST",
            "/api/v1/workflows",
            {"name": "x", "definition": {"nodes": [{"key": "a", "task_type": "sleep"}]}},
        ),
        ("POST", f"/api/v1/workflows/{uuid.uuid4()}/runs", {}),
    ):
        response = await demo.request(method, path, json=body)
        assert (response.status_code, code(response)) == (403, "disabled_in_demo"), path
    assert (await demo.get("/api/v1/runs")).status_code == 200, "reads stay open"


# ------------------------------------------------------------------ runs & ownership


async def test_demo_runs_are_paced_scoped_and_capped(
    demo: httpx.AsyncClient, db: asyncpg.Connection
) -> None:
    first = await demo.post("/api/v1/demo/runs", headers=ME)
    assert first.status_code == 201
    run = first.json()
    assert (run["trigger"], run["workflow_name"]) == ("demo", "order-pipeline")
    assert (
        await db.fetchval(
            "SELECT sandbox_id FROM workflow_runs WHERE id = $1", uuid.UUID(run["id"])
        )
        == ME["X-Sandbox-Id"]
    )
    chaos = await db.fetch(
        "SELECT node_key, chaos FROM task_instances WHERE run_id = $1", uuid.UUID(run["id"])
    )
    assert all(json.loads(r["chaos"]) == {"delay_s": 0.0} for r in chaos), "paced, no faults"

    assert (await demo.post("/api/v1/demo/runs", headers=ME)).status_code == 201
    capped = await demo.post("/api/v1/demo/runs", headers=ME)
    assert (capped.status_code, code(capped)) == (429, "rate_limited")
    assert int(capped.headers["retry-after"]) >= 1
    assert (await demo.post("/api/v1/demo/runs", headers=OTHER)).status_code == 201, "per sandbox"

    theirs = await demo.post(f"/api/v1/runs/{run['id']}/cancel", headers=OTHER)
    assert (theirs.status_code, code(theirs)) == (403, "not_your_run")
    mine = await demo.post(f"/api/v1/runs/{run['id']}/cancel", headers=ME)
    assert mine.json()["status"] == "cancelled"

    state = (await demo.get("/api/v1/demo/sandbox", headers=ME)).json()
    assert [r["status"] for r in state["runs"]] == ["running", "cancelled"]


async def test_failure_injection_retries_dead_letters_and_replay_fixes_it(
    demo: httpx.AsyncClient, engine: AsyncEngine, database_url: str, db: asyncpg.Connection
) -> None:
    assert (
        await demo.post("/api/v1/demo/chaos/toggle-failures", headers=ME, json={"enabled": True})
    ).json() == {"enabled": True}
    run = (await demo.post("/api/v1/demo/runs", headers=ME)).json()
    run_id = uuid.UUID(run["id"])
    # No API is running in-process for `fetch` to call: feed `validate` its data directly.
    await db.execute(
        "UPDATE task_instances SET task_type = 'sleep', params = '{\"seconds\": 0}' "
        "WHERE run_id = $1 AND node_key = 'fetch'",
        run_id,
    )
    await db.execute(
        "UPDATE task_instances SET params = jsonb_set(params, '{data}', $2::jsonb) "
        "WHERE run_id = $1 AND node_key = 'validate'",
        run_id,
        json.dumps([{"id": "o", "customer": "c", "total": 99, "status": "paid"}]),
    )
    await db.execute(
        "UPDATE task_instances SET backoff_policy = $2::jsonb WHERE run_id = $1",
        run_id,
        json.dumps({"base_s": 0.05, "max_s": 0.05, "jitter": "none"}),
    )
    config = WorkerConfig(
        concurrency=4,
        lease_ttl_s=3,
        heartbeat_interval_s=0.2,
        poll_interval_s=0.05,
        listen=False,
        demo_chaos=True,
    )
    worker = Worker(engine=engine, registry=REGISTRY, config=config, database_url=database_url)
    runner = asyncio.create_task(worker.run())
    try:

        async def status_is(want: str) -> bool:
            return bool(
                await db.fetchval("SELECT status FROM workflow_runs WHERE id = $1", run_id) == want
            )

        await eventually(
            lambda: status_is("failed"), within=20, what="run to stop on the dead letter"
        )
        rows = {
            r["node_key"]: r
            for r in await db.fetch(
                "SELECT node_key, status, attempt FROM task_instances WHERE run_id = $1", run_id
            )
        }
        assert (rows["transform"]["status"], rows["transform"]["attempt"]) == ("succeeded", 3)
        assert rows["notify"]["status"] == "dead"
        error = await db.fetchval(
            "SELECT error->>'message' FROM task_instances "
            "WHERE run_id = $1 AND node_key = 'notify'",
            run_id,
        )
        assert "injected by the demo" in error

        letter = (await demo.get("/api/v1/dead-letters")).json()["items"][0]
        theirs = await demo.post(f"/api/v1/dead-letters/{letter['id']}/replay", headers=OTHER)
        assert theirs.status_code == 403
        assert (
            await demo.post(f"/api/v1/dead-letters/{letter['id']}/replay", headers=ME)
        ).status_code == 200
        await eventually(lambda: status_is("succeeded"), within=20, what="replayed run to succeed")
        assert await db.fetchval("SELECT count(*) FROM notifications") == 1
    finally:
        worker.request_stop()
        await asyncio.wait_for(runner, timeout=10)


# ------------------------------------------------------------------ flood, kill, limits


async def test_flood_enqueues_both_bands_and_respects_the_queue_cap(
    demo: httpx.AsyncClient, db: asyncpg.Connection
) -> None:
    assert (await demo.post("/api/v1/demo/chaos/flood", headers=ME)).json() == {"low": 8, "high": 2}
    bands = {
        r["p"]: r["n"]
        for r in await db.fetch(
            "SELECT priority AS p, count(*) AS n FROM task_instances GROUP BY 1"
        )
    }
    assert bands == {-20: 8, 60: 2}
    state = (await demo.get("/api/v1/demo/sandbox", headers=ME)).json()
    assert state["flood"]["low"] == {"ready": 8}
    assert state["flood"]["high"] == {"ready": 2}
    assert (await demo.post("/api/v1/demo/chaos/flood", headers=ME)).status_code == 200
    capped = await demo.post("/api/v1/demo/chaos/flood", headers=ME)
    assert (capped.status_code, code(capped)) == (429, "rate_limited")


async def test_kill_targets_a_chaos_worker_running_my_task(
    demo: httpx.AsyncClient, db: asyncpg.Connection
) -> None:
    none = await demo.post("/api/v1/demo/chaos/kill-worker", headers=ME)
    assert (none.status_code, code(none)) == (409, "demo_unavailable")

    run = (await demo.post("/api/v1/demo/runs", headers=ME)).json()
    bystander, target, plain = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    for wid, chaos in ((bystander, True), (target, True), (plain, False)):
        await db.execute(
            "INSERT INTO workers (id, hostname, pid, concurrency, accepts_chaos) "
            "VALUES ($1, $2, 1, 2, $3)",
            wid,
            f"host-{wid.hex[:4]}",
            chaos,
        )
    await db.execute(
        "UPDATE task_instances SET status = 'running', lease_owner = $1, "
        "lease_expires_at = now() + interval '1 minute' WHERE run_id = $2 AND node_key = 'fetch'",
        target,
        uuid.UUID(run["id"]),
    )
    await asyncio.sleep(10.1)  # the per-IP kill cooldown from the attempt above
    killed = await demo.post("/api/v1/demo/chaos/kill-worker", headers=ME)
    assert killed.status_code == 200, killed.text
    assert killed.json()["worker_id"] == str(target)
    assert killed.json()["tasks"] == ["fetch"]
    assert (
        await db.fetchval(
            "SELECT count(*) FROM worker_commands WHERE worker_id = $1 AND command = 'kill'", target
        )
        == 1
    )
    assert (
        await db.fetchval("SELECT count(*) FROM run_events WHERE type = 'chaos_worker_killed'") == 1
    )

    again = await demo.post("/api/v1/demo/chaos/kill-worker", headers=ME)
    assert (again.status_code, code(again)) == (429, "rate_limited"), "cooldown between kills"


async def test_per_ip_action_budget(database_url: str, engine: AsyncEngine) -> None:
    await seed(engine, dataset_url="http://127.0.0.1:9/unused")
    app = create_app(
        Settings(database_url=database_url, demo_mode=True, demo_actions_per_ip_per_min=3)
    )
    async with make_client(app) as client:
        reads = [
            (await client.get("/api/v1/demo/sandbox", headers=ME)).status_code for _ in range(6)
        ]
        writes = [
            (
                await client.post(
                    "/api/v1/demo/chaos/toggle-failures", headers=ME, json={"enabled": True}
                )
            ).status_code
            for _ in range(4)
        ]
    await app.state.engine.dispose()
    assert reads == [200] * 6, "polling the sandbox must not spend the action budget"
    assert writes == [200, 200, 200, 429]


async def test_reset_removes_only_old_finished_data(
    demo: httpx.AsyncClient, engine: AsyncEngine, db: asyncpg.Connection
) -> None:
    old = (await demo.post("/api/v1/demo/runs", headers=ME)).json()
    await demo.post(f"/api/v1/runs/{old['id']}/cancel", headers=ME)
    live = (await demo.post("/api/v1/demo/runs", headers=ME)).json()
    await db.execute(
        "UPDATE workflow_runs SET finished_at = now() - interval '2 hours', "
        "created_at = now() - interval '2 hours' WHERE id = $1",
        uuid.UUID(old["id"]),
    )
    async with engine.begin() as conn:
        deleted = await service.reset_demo_data(conn, retention_s=3600)
    assert deleted["runs"] == 1
    remaining = [str(r["id"]) for r in await db.fetch("SELECT id FROM workflow_runs")]
    assert remaining == [live["id"]]

"""The REST surface, end to end against a real PostgreSQL."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from typing import Any

import asyncpg
import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from quorum.domain.retry import FailureKind
from quorum.samples import sample_definition
from quorum.store import tasks as task_store
from tests.integration.http_fixture import local_http_server


@pytest.fixture
def base_url() -> Iterator[str]:
    with local_http_server() as url:
        yield url


def workflow_body(name: str, url: str, **extra: Any) -> dict[str, Any]:
    return {"name": name, "definition": sample_definition(url).model_dump(mode="json"), **extra}


def error_of(response: httpx.Response) -> dict[str, Any]:
    body: dict[str, Any] = response.json()["error"]
    assert body["request_id"] == response.headers["x-request-id"]
    return body


# ------------------------------------------------------------------ workflows


async def test_workflow_lifecycle(client: httpx.AsyncClient, base_url: str) -> None:
    created = await client.post(
        "/api/v1/workflows",
        json=workflow_body(
            "orders", f"{base_url}/orders", cron="0 9 * * MON-FRI", timezone="Europe/London"
        ),
    )
    assert created.status_code == 201, created.text
    wf = created.json()
    assert (wf["version"], wf["cron"], wf["timezone"]) == (1, "0 9 * * MON-FRI", "Europe/London")
    assert wf["next_fire_at"] is not None

    dup = await client.post("/api/v1/workflows", json=workflow_body("orders", f"{base_url}/x"))
    assert dup.status_code == 409
    assert error_of(dup)["code"] == "name_taken"

    got = await client.get(f"/api/v1/workflows/{wf['id']}")
    assert got.json()["definition"]["nodes"][0]["key"] == "fetch"

    updated = await client.put(
        f"/api/v1/workflows/{wf['id']}",
        json={"definition": wf["definition"], "cron": None, "timezone": "UTC", "enabled": True},
    )
    assert (updated.json()["version"], updated.json()["next_fire_at"]) == (2, None)

    page = (await client.get("/api/v1/workflows", params={"limit": 1})).json()
    assert (page["total"], page["limit"], page["offset"], len(page["items"])) == (1, 1, 0, 1)

    missing = await client.get(f"/api/v1/workflows/{uuid.uuid4()}")
    assert missing.status_code == 404
    assert error_of(missing)["code"] == "not_found"


async def test_invalid_workflows_are_rejected_with_every_problem(client: httpx.AsyncClient) -> None:
    body = {
        "name": "broken",
        "definition": {
            "nodes": [
                {"key": "a", "task_type": "sleep", "depends_on": ["b"]},
                {"key": "b", "task_type": "sleep", "depends_on": ["a"]},
            ]
        },
    }
    response = await client.post("/api/v1/workflows", json=body)
    assert response.status_code == 422
    error = error_of(response)
    assert error["code"] == "invalid_workflow"
    assert error["details"] == ["cycle detected: a -> b -> a"]

    body = {
        "name": "params",
        "definition": {"nodes": [{"key": "a", "task_type": "sleep", "params": {"seconds": -1}}]},
    }
    response = await client.post("/api/v1/workflows", json=body)
    assert "a: invalid params" in error_of(response)["details"][0]

    body = {
        "name": "cron",
        "definition": {"nodes": [{"key": "a", "task_type": "sleep"}]},
        "cron": "nope",
    }
    response = await client.post("/api/v1/workflows", json=body)
    assert error_of(response)["code"] == "invalid_schedule"

    response = await client.post(
        "/api/v1/workflows", json={"name": "Bad Name", "definition": {"nodes": []}}
    )
    assert error_of(response)["code"] == "validation_error"


# ------------------------------------------------------------------ runs


async def test_trigger_inspect_list_and_cancel_runs(
    client: httpx.AsyncClient, base_url: str
) -> None:
    wf = (
        await client.post("/api/v1/workflows", json=workflow_body("p", f"{base_url}/orders"))
    ).json()
    triggered = await client.post(f"/api/v1/workflows/{wf['id']}/runs")
    assert triggered.status_code == 201
    run = triggered.json()
    assert (run["status"], run["trigger"], run["task_count"], run["workflow_name"]) == (
        "running",
        "manual",
        6,
        "p",
    )

    detail = (await client.get(f"/api/v1/runs/{run['id']}")).json()
    by_key = {t["node_key"]: t for t in detail["tasks"]}
    assert by_key["notify"]["depends_on"] == ["report", "store"]
    assert by_key["fetch"]["status"] == "ready"
    assert [e["type"] for e in detail["events"]][:2] == ["run_created", "task_ready"]

    listed = (await client.get("/api/v1/runs", params={"status": "running"})).json()
    assert [r["id"] for r in listed["items"]] == [run["id"]]
    assert (await client.get("/api/v1/runs", params={"status": "succeeded"})).json()["total"] == 0
    bad = await client.get("/api/v1/runs", params={"status": "exploded"})
    assert bad.status_code == 422

    cancelled = await client.post(f"/api/v1/runs/{run['id']}/cancel")
    assert cancelled.json()["status"] == "cancelled"
    again = await client.post(f"/api/v1/runs/{run['id']}/cancel")
    assert (again.status_code, error_of(again)["code"]) == (409, "not_cancellable")

    assert (await client.post(f"/api/v1/workflows/{uuid.uuid4()}/runs")).status_code == 404
    assert (await client.get(f"/api/v1/runs/{uuid.uuid4()}")).status_code == 404


async def test_run_list_pagination(client: httpx.AsyncClient) -> None:
    for _ in range(5):
        await client.post("/api/v1/jobs", json={"task_type": "sleep", "params": {"seconds": 0}})
    first = (await client.get("/api/v1/runs", params={"limit": 2})).json()
    second = (await client.get("/api/v1/runs", params={"limit": 2, "offset": 2})).json()
    assert first["total"] == 5
    assert not {r["id"] for r in first["items"]} & {r["id"] for r in second["items"]}
    assert (await client.get("/api/v1/runs", params={"limit": 101})).status_code == 422


# ------------------------------------------------------------------ jobs


async def test_jobs_validate_and_dedupe(client: httpx.AsyncClient) -> None:
    ok = await client.post(
        "/api/v1/jobs",
        json={"task_type": "notify", "params": {"message": "hi"}, "idempotency_key": "order-7"},
    )
    assert ok.status_code == 201
    assert ok.json()["idempotency_key"] == "order-7"

    dup = await client.post(
        "/api/v1/jobs",
        json={"task_type": "notify", "params": {"message": "hi"}, "idempotency_key": "order-7"},
    )
    assert (dup.status_code, error_of(dup)["code"]) == (409, "duplicate_idempotency_key")

    unknown = await client.post("/api/v1/jobs", json={"task_type": "shell", "params": {}})
    assert error_of(unknown)["code"] == "unknown_task_type"

    bad = await client.post("/api/v1/jobs", json={"task_type": "sleep", "params": {"seconds": 999}})
    error = error_of(bad)
    assert error["code"] == "invalid_params"
    assert error["details"][0]["loc"] == ["seconds"]

    priority = await client.post("/api/v1/jobs", json={"task_type": "sleep", "priority": 101})
    assert error_of(priority)["code"] == "validation_error"

    key = await client.post(
        "/api/v1/jobs", json={"task_type": "sleep", "idempotency_key": "has space"}
    )
    assert error_of(key)["code"] == "invalid_job"


# ------------------------------------------------------------------ workers & dead letters


async def test_dead_letter_listing_and_replay(
    client: httpx.AsyncClient, engine: AsyncEngine, db: asyncpg.Connection
) -> None:
    job = (await client.post("/api/v1/jobs", json={"task_type": "sleep", "max_attempts": 1})).json()
    worker = uuid.uuid4()
    async with engine.begin() as conn:
        [claim] = await task_store.claim_tasks(conn, worker_id=worker, limit=1, lease_ttl_s=30)
        await task_store.fail_task(
            conn,
            task_id=claim.id,
            worker_id=worker,
            fencing_token=claim.fencing_token,
            kind=FailureKind.ERROR,
            error={"message": "boom"},
        )

    letters = (await client.get("/api/v1/dead-letters")).json()
    assert letters["total"] == 1
    letter = letters["items"][0]
    assert (letter["task_id"], letter["task_type"], letter["payload"]["error"]["message"]) == (
        job["task_id"],
        "sleep",
        "boom",
    )

    replayed = await client.post(f"/api/v1/dead-letters/{letter['id']}/replay")
    assert replayed.json() == {"task_id": job["task_id"], "run_id": job["run_id"]}
    assert (await client.get("/api/v1/dead-letters")).json()["total"] == 0
    assert (await client.get("/api/v1/dead-letters", params={"include_replayed": True})).json()[
        "total"
    ] == 1

    again = await client.post(f"/api/v1/dead-letters/{letter['id']}/replay")
    assert again.status_code == 404

    await db.execute(
        "INSERT INTO workers (id, hostname, pid, concurrency) VALUES ($1, 'host-a', 42, 4)", worker
    )
    from quorum.api.hub import dumps
    from quorum.store import queries

    async with engine.connect() as conn:
        snapshot = json.loads(dumps(await queries.list_workers(conn)))
    assert isinstance(snapshot[0]["heartbeat_age_s"], float), "live snapshot sends a number"
    workers = (await client.get("/api/v1/workers")).json()
    assert [(w["hostname"], w["status"], w["current_tasks"]) for w in workers] == [
        ("host-a", "active", [])
    ]


# ------------------------------------------------------------------ overview & metrics


async def test_stats_task_types_and_metrics(client: httpx.AsyncClient) -> None:
    await client.post("/api/v1/jobs", json={"task_type": "sleep"})
    stats = (await client.get("/api/v1/stats")).json()
    assert stats["tasks"]["queued"] == 1
    assert len(stats["throughput"]) >= 30
    assert stats["scheduler"] is None

    types = {t["name"]: t for t in (await client.get("/api/v1/task-types")).json()}
    assert types["notify"]["has_side_effects"]
    assert "message" in types["notify"]["params_schema"]["required"]

    for path in ("/api/v1/metrics", "/metrics"):
        response = await client.get(path)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain; version=0.0.4")
        body = response.text
        assert 'quorum_tasks{status="ready"} 1' in body
        assert "# TYPE quorum_lease_expirations_total counter" in body
        assert 'quorum_task_duration_seconds{quantile="0.95"}' in body

    openapi = (await client.get("/openapi.json")).json()
    for path in ("/api/v1/workflows", "/api/v1/runs/{run_id}", "/api/v1/stream", "/api/v1/jobs"):
        assert path in openapi["paths"]


async def test_cors_allows_the_dashboard_origin(client: httpx.AsyncClient) -> None:
    response = await client.options(
        "/api/v1/stats",
        headers={"Origin": "http://localhost:3000", "Access-Control-Request-Method": "GET"},
    )
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"
    denied = await client.options(
        "/api/v1/stats",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"},
    )
    assert "access-control-allow-origin" not in denied.headers


async def test_seed_is_idempotent_and_the_dataset_is_served(
    client: httpx.AsyncClient, engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    from quorum.seed import seed

    caplog.set_level("INFO")  # exercise the log calls too (a bad `extra` key raises)
    assert await seed(engine, dataset_url="http://localhost:8000/api/v1/demo/dataset")
    assert not await seed(engine, dataset_url="http://localhost:8000/api/v1/demo/dataset")
    workflows = (await client.get("/api/v1/workflows")).json()
    assert [w["name"] for w in workflows["items"]] == ["order-pipeline"]
    dataset = (await client.get("/api/v1/demo/dataset")).json()
    assert len(dataset) == 8
    assert {"id", "customer", "total", "status"} <= set(dataset[0])

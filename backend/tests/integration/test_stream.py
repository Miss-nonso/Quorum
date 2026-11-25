"""SSE against a real uvicorn server (in-process ASGI transports buffer streams)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import uvicorn

from quorum.api.app import create_app
from quorum.core.config import Settings
from tests.conftest import eventually


@pytest.fixture
async def server(settings: Settings, db: object) -> AsyncIterator[str]:
    app = create_app(settings)
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_config=None, lifespan="on")
    srv = uvicorn.Server(config)
    task = asyncio.create_task(srv.serve())

    async def started() -> bool:
        return bool(srv.started)

    await eventually(started, within=10, what="uvicorn to start")
    port = srv.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        srv.should_exit = True
        await asyncio.wait_for(task, timeout=10)


class SSEReader:
    """Parses one SSE response; keeps a single line iterator across reads."""

    def __init__(self, response: httpx.Response) -> None:
        self._lines = response.aiter_lines()

    async def read(self, *, until: str, count: int = 1, within: float = 10) -> list[dict[str, Any]]:
        """Collect messages until ``count`` messages of type ``until`` have arrived."""
        messages: list[dict[str, Any]] = []
        current: dict[str, Any] = {}
        seen = 0
        async with asyncio.timeout(within):
            while True:
                line = await anext(self._lines)
                if line == "":
                    if current:
                        messages.append(current)
                        if current.get("event") == until:
                            seen += 1
                            if seen >= count:
                                return messages
                    current = {}
                elif line.startswith(":"):
                    continue
                else:
                    name, _, value = line.partition(": ")
                    if name == "data":
                        current["data"] = json.loads(value)
                    elif name == "id":
                        current["id"] = int(value)
                    else:
                        current[name] = value


async def test_stream_delivers_live_events_and_replays_on_reconnect(server: str) -> None:
    async with httpx.AsyncClient(base_url=server, timeout=10) as client:
        async with client.stream("GET", "/api/v1/stream") as response:
            assert response.headers["content-type"].startswith("text/event-stream")
            sse = SSEReader(response)
            opening = await sse.read(until="stats")
            assert [m["event"] for m in opening] == ["hello", "workers", "stats"]

            job = (await client.post("/api/v1/jobs", json={"task_type": "sleep"})).json()
            [enqueued] = [m for m in await sse.read(until="run_event") if m["event"] == "run_event"]
            assert enqueued["data"]["type"] == "task_enqueued"
            assert enqueued["data"]["task_id"] == job["task_id"]
            last_id = enqueued["id"]

        # Disconnected: these happen while nobody is watching.
        missed = [
            (await client.post("/api/v1/jobs", json={"task_type": "sleep"})).json()
            for _ in range(3)
        ]

        headers = {"Last-Event-ID": str(last_id)}
        async with client.stream("GET", "/api/v1/stream", headers=headers) as response:
            replay = await SSEReader(response).read(until="run_event", count=3)
            events = [m for m in replay if m["event"] == "run_event"]
            assert [e["data"]["task_id"] for e in events] == [j["task_id"] for j in missed]
            ids = [e["id"] for e in events]
            assert ids == sorted(ids)
            assert ids[0] > last_id, "nothing replayed twice"
            assert replay[0]["data"]["resumed_from"] == last_id


async def test_stream_can_be_filtered_to_one_run(server: str) -> None:
    async with httpx.AsyncClient(base_url=server, timeout=10) as client:
        mine = (await client.post("/api/v1/jobs", json={"task_type": "sleep"})).json()
        params = {"run_id": mine["run_id"], "after": 0}
        async with client.stream("GET", "/api/v1/stream", params=params) as response:
            sse = SSEReader(response)
            await client.post("/api/v1/jobs", json={"task_type": "sleep"})  # someone else's
            replay = await sse.read(until="run_event")
            [event] = [m for m in replay if m["event"] == "run_event"]
            assert event["data"]["run_id"] == mine["run_id"]
            # The other run's event must not arrive: wait briefly and check.
            with pytest.raises(TimeoutError):
                await sse.read(until="run_event", within=2.5)


async def test_fresh_connections_can_ask_for_recent_history(server: str) -> None:
    async with httpx.AsyncClient(base_url=server, timeout=10) as client:
        jobs = [
            (await client.post("/api/v1/jobs", json={"task_type": "sleep"})).json()
            for _ in range(5)
        ]
        async with client.stream("GET", "/api/v1/stream", params={"recent": 3}) as response:
            replay = await SSEReader(response).read(until="run_event", count=3)
            events = [m for m in replay if m["event"] == "run_event"]
            assert [e["data"]["task_id"] for e in events] == [j["task_id"] for j in jobs[-3:]]

"""SIGTERM must stop the API promptly even while SSE clients are connected."""

from __future__ import annotations

import asyncio
import contextlib
import signal
import socket
import time

import asyncpg
import httpx

from tests.chaos.conftest import Cluster
from tests.conftest import eventually


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def test_api_exits_within_grace_period_despite_open_streams(
    cluster: Cluster, db: asyncpg.Connection
) -> None:
    cluster.extra_env["CONDUCTOR_SHUTDOWN_GRACE_S"] = "2"
    port = free_port()
    api = cluster.api(port)
    base = f"http://127.0.0.1:{port}"

    async def healthy() -> bool:
        try:
            async with httpx.AsyncClient() as c:
                return (await c.get(f"{base}/api/v1/health")).status_code == 200
        except httpx.TransportError:
            return False

    await eventually(healthy, within=20, what="API to start")

    async with httpx.AsyncClient(base_url=base, timeout=30) as client:
        streams = [client.stream("GET", "/api/v1/stream") for _ in range(3)]
        opened = [await s.__aenter__() for s in streams]  # three idle SSE clients
        assert all(r.status_code == 200 for r in opened)

        api.signal(signal.SIGTERM)
        started = time.monotonic()
        # Wait in a thread so the event loop stays free.
        await asyncio.to_thread(lambda: api.popen.wait(timeout=15))
        elapsed = time.monotonic() - started
        for s in streams:
            with contextlib.suppress(httpx.HTTPError):
                await s.__aexit__(None, None, None)

    assert api.popen.returncode is not None, "API ignored SIGTERM while streams were open"
    assert elapsed < 6, f"took {elapsed:.1f}s; graceful shutdown should be bounded at ~2s"

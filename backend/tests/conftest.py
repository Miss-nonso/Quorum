"""Shared fixtures.

Unit tests never touch a database. Integration tests (``tests/integration``)
run against a real PostgreSQL named by ``TEST_DATABASE_URL``; a fresh
database is created and migrated once per test session.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI

from conductor.api.app import create_app
from conductor.core.config import Settings

# Port 1 is reserved and never listening: connections fail immediately.
UNREACHABLE_DB_URL = "postgresql+asyncpg://nobody:nobody@127.0.0.1:1/none"


def make_client(app: FastAPI) -> httpx.AsyncClient:
    """An HTTP client that calls the ASGI app in-process."""
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


@pytest.fixture
def offline_settings() -> Settings:
    """Settings whose database can never be reached."""
    return Settings(database_url=UNREACHABLE_DB_URL, db_connect_timeout_s=1.0)


@pytest.fixture
async def offline_client(offline_settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    """Client for an app with no reachable database (for pure API behaviour)."""
    app = create_app(offline_settings)
    async with make_client(app) as client:
        yield client
    await app.state.engine.dispose()

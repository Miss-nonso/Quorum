"""Shared fixtures.

Unit tests (``tests/unit``) never touch a database. Integration and chaos
tests run against a real PostgreSQL named by ``TEST_DATABASE_URL``: a fresh
database is created and migrated once per session, every engine table is
truncated before each test, and the database is dropped at the end. If the
server is unreachable those tests fail; they are never skipped, because a
silently skipped integration suite proves nothing.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from pathlib import Path

import asyncpg
import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from conductor.api.app import create_app
from conductor.core.config import Settings
from conductor.core.db import asyncpg_dsn

# Port 1 is reserved and never listening: connections fail immediately.
UNREACHABLE_DB_URL = "postgresql+asyncpg://nobody:nobody@127.0.0.1:1/none"
DEFAULT_TEST_URL = "postgresql+asyncpg://conductor:conductor@localhost:5433/conductor"
BACKEND_DIR = Path(__file__).resolve().parents[1]
ENGINE_TABLES = (
    "notifications",
    "dead_letters",
    "run_events",
    "task_dependencies",
    "task_instances",
    "workflow_runs",
    "workflows",
    "workers",
    "schedules",
    "scheduler_state",
    "stored_records",
)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    tests_dir = Path(__file__).parent
    for item in items:
        path = Path(str(item.fspath))
        if path.is_relative_to(tests_dir / "integration"):
            item.add_marker(pytest.mark.integration)
        elif path.is_relative_to(tests_dir / "chaos"):
            item.add_marker(pytest.mark.integration)
            item.add_marker(pytest.mark.chaos)


def make_client(app: FastAPI) -> httpx.AsyncClient:
    """An HTTP client that calls the ASGI app in-process."""
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


async def eventually[T](
    probe: Callable[[], Awaitable[T | None]],
    *,
    within: float = 10.0,
    interval: float = 0.05,
    what: str = "condition",
) -> T:
    """Poll ``probe`` until it returns something truthy; fail after ``within`` seconds."""
    deadline = time.monotonic() + within
    while True:
        value = await probe()
        if value:
            return value
        if time.monotonic() > deadline:
            pytest.fail(f"timed out after {within}s waiting for {what}")
        await asyncio.sleep(interval)


# ------------------------------------------------------------------ offline API


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


# ------------------------------------------------------------------ real PostgreSQL


async def _admin(url: str, sql: str) -> None:
    conn = await asyncpg.connect(asyncpg_dsn(url))
    try:
        await conn.execute(sql)
    finally:
        await conn.close()


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    """URL of a freshly created and migrated database for this test session."""
    server_url = make_url(os.environ.get("TEST_DATABASE_URL", DEFAULT_TEST_URL))
    name = f"{server_url.database}_test_{secrets.token_hex(4)}"
    maintenance = server_url.set(database="postgres").render_as_string(hide_password=False)
    test_url = server_url.set(database=name).render_as_string(hide_password=False)

    asyncio.run(_admin(maintenance, f'CREATE DATABASE "{name}"'))
    try:
        cfg = Config(str(BACKEND_DIR / "alembic.ini"))
        cfg.set_main_option("sqlalchemy.url", test_url.replace("%", "%%"))
        command.upgrade(cfg, "head")
        yield test_url
    finally:
        asyncio.run(_admin(maintenance, f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


@pytest.fixture
async def db(database_url: str) -> AsyncIterator[asyncpg.Connection]:
    """A raw asyncpg connection to the test database, with engine tables emptied."""
    conn = await asyncpg.connect(asyncpg_dsn(database_url))
    await conn.execute(f"TRUNCATE {', '.join(ENGINE_TABLES)} CASCADE")
    try:
        yield conn
    finally:
        await conn.close()


@pytest.fixture
async def engine(database_url: str, db: asyncpg.Connection) -> AsyncIterator[AsyncEngine]:
    """SQLAlchemy engine on the (freshly truncated) test database."""
    eng = create_async_engine(database_url, pool_size=30, max_overflow=0)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest.fixture
def settings(database_url: str) -> Settings:
    return Settings(database_url=database_url)


@pytest.fixture
async def client(settings: Settings, db: asyncpg.Connection) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(settings)
    async with make_client(app) as c:
        yield c
    await app.state.engine.dispose()

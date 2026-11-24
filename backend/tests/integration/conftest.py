"""Real-PostgreSQL fixtures.

A throwaway database (``<name>_test_<random>``) is created on the server in
``TEST_DATABASE_URL``, migrated to head once per session, and dropped at the
end. If the server is unreachable the tests fail: they are never skipped,
because a silently skipped integration suite proves nothing.
"""

from __future__ import annotations

import asyncio
import os
import secrets
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import asyncpg
import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url

from conductor.api.app import create_app
from conductor.core.config import Settings
from tests.conftest import make_client

DEFAULT_TEST_URL = "postgresql+asyncpg://conductor:conductor@localhost:5433/conductor"
BACKEND_DIR = Path(__file__).resolve().parents[2]


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if Path(str(item.fspath)).is_relative_to(Path(__file__).parent):
            item.add_marker(pytest.mark.integration)


async def _admin(url: str, sql: str) -> None:
    conn = await asyncpg.connect(
        make_url(url).set(drivername="postgresql").render_as_string(hide_password=False)
    )
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
def settings(database_url: str) -> Settings:
    return Settings(database_url=database_url)


@pytest.fixture
async def client(settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(settings)
    async with make_client(app) as c:
        yield c
    await app.state.engine.dispose()


@pytest.fixture
async def db(database_url: str) -> AsyncIterator[asyncpg.Connection]:
    """A raw asyncpg connection to the test database."""
    conn = await asyncpg.connect(
        make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)
    )
    try:
        yield conn
    finally:
        await conn.close()

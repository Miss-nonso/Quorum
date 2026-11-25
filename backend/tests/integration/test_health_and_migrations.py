from __future__ import annotations

import asyncpg
import httpx
from alembic.config import Config
from alembic.script import ScriptDirectory

from conductor import __version__
from tests.conftest import BACKEND_DIR


async def test_health_is_ok_with_real_database(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "version": __version__,
        "checks": {"database": "ok"},
    }


async def test_migrations_are_at_head(db: asyncpg.Connection) -> None:
    scripts = ScriptDirectory.from_config(Config(str(BACKEND_DIR / "alembic.ini")))
    assert (
        await db.fetchval("SELECT version_num FROM alembic_version") == scripts.get_current_head()
    )


async def test_baseline_pins_database_timezone_to_utc(db: asyncpg.Connection) -> None:
    # ALTER DATABASE ... SET applies to new sessions; this fixture opens one.
    assert await db.fetchval("SHOW timezone") == "UTC"


async def test_server_meets_minimum_version(db: asyncpg.Connection) -> None:
    assert int(await db.fetchval("SHOW server_version_num")) >= 160000

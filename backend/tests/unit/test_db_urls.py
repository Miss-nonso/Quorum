"""Hosted-provider connection strings (Neon, Supabase, Render) must just work."""

from __future__ import annotations

import pytest

from conductor.core.config import Settings
from conductor.core.db import asyncpg_dsn, engine_args

NEON = (
    "postgresql://app:s3cret@ep-cool-name-123456.eu-central-1.aws.neon.tech/neondb"
    "?sslmode=require&channel_binding=require"
)


@pytest.mark.parametrize("scheme", ["postgres://", "postgresql://", "postgresql+asyncpg://"])
def test_plain_provider_urls_get_the_asyncpg_driver(scheme: str) -> None:
    settings = Settings(database_url=f"{scheme}u:p@db.example.com:5432/app")
    assert settings.database_url == "postgresql+asyncpg://u:p@db.example.com:5432/app"


def test_non_postgres_urls_are_rejected() -> None:
    with pytest.raises(ValueError, match="PostgreSQL"):
        Settings(database_url="mysql://u:p@h/db")


def test_neon_url_is_translated_for_sqlalchemy_and_asyncpg() -> None:
    settings = Settings(database_url=NEON)
    url, connect_args = engine_args(settings.database_url)
    assert "sslmode" not in url
    assert "channel_binding" not in url
    assert url.startswith("postgresql+asyncpg://app:s3cret@ep-cool-name")
    assert connect_args == {"ssl": "require"}

    dsn = asyncpg_dsn(settings.database_url)
    assert dsn.startswith("postgresql://")
    assert "sslmode=require" in dsn, "asyncpg understands sslmode in a DSN"
    assert "channel_binding" not in dsn


def test_sslmode_disable_turns_tls_off() -> None:
    _, connect_args = engine_args("postgresql+asyncpg://u:p@h/db?sslmode=disable")
    assert connect_args == {"ssl": False}


def test_local_urls_pass_through_untouched() -> None:
    local = "postgresql+asyncpg://conductor:conductor@localhost:5433/conductor"
    assert engine_args(local) == (local, {})

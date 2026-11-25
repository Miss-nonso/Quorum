from __future__ import annotations

import pytest

from quorum.core.config import LogFormat, Settings


def test_defaults_target_local_development() -> None:
    settings = Settings.from_env({})
    assert settings.env == "development"
    assert settings.database_url.startswith("postgresql+asyncpg://")
    assert settings.log_format is LogFormat.JSON


def test_reads_prefixed_variables_and_ignores_others() -> None:
    settings = Settings.from_env(
        {
            "QUORUM_API_PORT": "9001",
            "QUORUM_LOG_FORMAT": "text",
            "PATH": "/usr/bin",  # unrelated variables are not our business
        }
    )
    assert settings.api_port == 9001
    assert settings.log_format is LogFormat.TEXT


def test_unknown_quorum_variable_is_rejected() -> None:
    with pytest.raises(ValueError, match="databse_url"):
        Settings.from_env({"QUORUM_DATABSE_URL": "postgresql+asyncpg://x"})


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("QUORUM_API_PORT", "0"),
        ("QUORUM_DB_POOL_SIZE", "-1"),
        ("QUORUM_LOG_FORMAT", "xml"),
    ],
)
def test_invalid_values_are_rejected(key: str, value: str) -> None:
    with pytest.raises(ValueError, match="Invalid Quorum configuration"):
        Settings.from_env({key: value})


def test_settings_are_immutable() -> None:
    settings = Settings.from_env({})
    with pytest.raises(ValueError, match="frozen"):
        settings.api_port = 1  # type: ignore[misc]

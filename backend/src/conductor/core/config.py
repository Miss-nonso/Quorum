"""Process configuration, read once from environment variables.

Every setting has a ``CONDUCTOR_`` prefixed variable. Defaults target local
development; production values come from the environment only (never files
committed to the repo). See ``.env.example`` at the repository root.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from enum import StrEnum
from functools import lru_cache

from pydantic import BaseModel, ConfigDict, Field, ValidationError

ENV_PREFIX = "CONDUCTOR_"


class LogFormat(StrEnum):
    """Output format for application logs."""

    JSON = "json"
    TEXT = "text"


class Settings(BaseModel):
    """Validated runtime settings shared by the API, scheduler and worker."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    env: str = Field(default="development", description="Deployment environment name.")
    database_url: str = Field(
        default="postgresql+asyncpg://conductor:conductor@localhost:5433/conductor",
        description="SQLAlchemy async URL. Must use the asyncpg driver.",
    )
    db_pool_size: int = Field(default=5, ge=1, le=100)
    db_connect_timeout_s: float = Field(default=5.0, gt=0)
    log_level: str = Field(default="INFO")
    log_format: LogFormat = Field(default=LogFormat.JSON)
    api_host: str = Field(default="127.0.0.1")
    api_port: int = Field(default=8000, ge=1, le=65535)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        """Build settings from ``CONDUCTOR_*`` variables.

        Unknown ``CONDUCTOR_*`` variables are rejected so that a typo such as
        ``CONDUCTOR_DATABSE_URL`` fails loudly instead of being ignored.

        Raises:
            ValueError: if a variable is unknown or a value fails validation.
        """
        source = os.environ if environ is None else environ
        values = {
            key.removeprefix(ENV_PREFIX).lower(): value
            for key, value in source.items()
            if key.startswith(ENV_PREFIX)
        }
        try:
            return cls.model_validate(values)
        except ValidationError as exc:
            raise ValueError(f"Invalid Conductor configuration:\n{exc}") from exc


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, loaded on first use."""
    return Settings.from_env()

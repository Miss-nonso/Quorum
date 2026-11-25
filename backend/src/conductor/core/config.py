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

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

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
    cors_origins: str = Field(
        default="http://localhost:3000",
        description="Comma-separated origins allowed to call the API from a browser.",
    )

    # --- worker
    worker_concurrency: int = Field(default=4, ge=1, le=64)
    lease_ttl_s: float = Field(default=30.0, gt=0, description="How long a claim lasts unrenewed.")
    heartbeat_interval_s: float = Field(default=5.0, gt=0)
    poll_interval_s: float = Field(
        default=2.0, gt=0, description="Max idle wait between claim attempts (LISTEN is a hint)."
    )
    shutdown_grace_s: float = Field(default=20.0, ge=0)
    worker_listen: bool = Field(default=True, description="Use LISTEN/NOTIFY to wake early.")

    # --- built-in task limits
    http_allowlist: str = Field(
        default="localhost,127.0.0.1",
        description="Comma-separated hosts (or host:port) that http_fetch may contact.",
    )
    http_timeout_s: float = Field(default=10.0, gt=0, le=60)
    http_max_bytes: int = Field(default=256 * 1024, ge=1, le=10 * 1024 * 1024)
    demo_dataset_url: str = Field(
        default="http://localhost:8000/api/v1/demo/dataset",
        description="Where the seeded sample workflow fetches its input (must be allowlisted).",
    )

    # --- scheduler / reaper
    reaper_interval_s: float = Field(default=5.0, gt=0)
    reaper_batch_size: int = Field(default=100, ge=1, le=10_000)
    worker_unhealthy_after_s: float = Field(default=15.0, gt=0)
    worker_dead_after_s: float = Field(default=60.0, gt=0)
    cron_tick_s: float = Field(default=1.0, gt=0)
    aging_tick_s: float = Field(default=5.0, gt=0)
    aging_interval_s: float = Field(
        default=10.0, gt=0, description="Seconds of waiting per +1 effective priority."
    )
    aging_max_bonus: int = Field(
        default=200,
        ge=0,
        le=1000,
        description="Cap on the aging bonus (>= 200 prevents starvation).",
    )
    leader_check_interval_s: float = Field(default=2.0, gt=0)
    leader_retry_interval_s: float = Field(default=2.0, gt=0)

    # --- all-in-one mode (`conductor all`, for free hosting tiers)
    all_in_one_workers: int = Field(default=2, ge=1, le=16)

    # --- public demo
    demo_mode: bool = Field(
        default=False,
        description="Expose /demo endpoints and disable general write endpoints (public demo).",
    )
    demo_chaos: bool = Field(
        default=False,
        description="Worker obeys demo kill commands and injected task faults. Demo pool only.",
    )
    demo_step_delay_s: float = Field(default=1.2, ge=0, le=10)
    demo_max_active_runs: int = Field(default=30, ge=1, le=1000)
    demo_max_runs_per_sandbox: int = Field(default=3, ge=1, le=50)
    demo_flood_low: int = Field(default=40, ge=1, le=500)
    demo_flood_high: int = Field(default=6, ge=1, le=100)
    demo_max_queued: int = Field(default=400, ge=10, le=10_000)
    demo_actions_per_ip_per_min: int = Field(default=20, ge=1, le=1000)
    demo_kills_per_min: int = Field(default=6, ge=1, le=600)
    demo_retention_s: float = Field(default=1800, gt=0)
    demo_reset_interval_s: float = Field(default=300, gt=0)

    @field_validator("database_url")
    @classmethod
    def _asyncpg_driver(cls, value: str) -> str:
        """Accept the plain URLs hosting providers hand out (postgres://, postgresql://)."""
        for prefix in ("postgres://", "postgresql://"):
            if value.startswith(prefix):
                return "postgresql+asyncpg://" + value[len(prefix) :]
        if not value.startswith("postgresql+asyncpg://"):
            raise ValueError("database_url must be a PostgreSQL URL")
        return value

    @model_validator(mode="after")
    def _check_timing(self) -> Settings:
        # At least two heartbeats per lease: one lost heartbeat must not cost a lease.
        if self.heartbeat_interval_s * 2 > self.lease_ttl_s:
            raise ValueError("heartbeat_interval_s must be at most half of lease_ttl_s")
        if self.worker_unhealthy_after_s >= self.worker_dead_after_s:
            raise ValueError("worker_unhealthy_after_s must be less than worker_dead_after_s")
        return self

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

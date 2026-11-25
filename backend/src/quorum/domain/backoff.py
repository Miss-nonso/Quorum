"""Retry delay calculation.

Exponential backoff with an upper cap and, by default, "full jitter": the
delay is drawn uniformly from ``[0, ceiling]``. Full jitter spreads retries
from a burst of simultaneous failures (a downstream outage, a killed worker
pool) instead of having them stampede back in lockstep.
"""

from __future__ import annotations

import math
import random
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class BackoffPolicy(BaseModel):
    """Per-task retry delay settings, stored as JSON on the task row."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    base_s: float = Field(default=1.0, gt=0, le=3600)
    factor: float = Field(default=2.0, ge=1, le=10)
    max_s: float = Field(default=300.0, gt=0, le=86_400)
    jitter: Literal["full", "none"] = "full"

    def ceiling(self, attempt: int) -> float:
        """Upper bound on the delay after failed attempt number ``attempt`` (1-based).

        Grows as ``base_s * factor ** (attempt - 1)`` and is capped at ``max_s``.
        """
        if attempt < 1:
            raise ValueError("attempt is 1-based")
        try:
            raw = self.base_s * self.factor ** (attempt - 1)
        except OverflowError:
            raw = math.inf
        return min(raw, self.max_s)

    def delay(self, attempt: int, rng: random.Random) -> float:
        """Seconds to wait before retrying after failed attempt ``attempt``."""
        ceiling = self.ceiling(attempt)
        return rng.uniform(0.0, ceiling) if self.jitter == "full" else ceiling

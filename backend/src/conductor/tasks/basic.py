"""``sleep``, ``flaky`` and ``notify``: the Phase 1 task types.

``sleep`` and ``flaky`` exist to make timing and failure visible in the demo
and in chaos tests. ``notify`` is the reference side-effecting task: it shows
how a task turns at-least-once execution into an effectively-once effect.
"""

from __future__ import annotations

import asyncio
import random
from string import Template
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from conductor.worker.registry import JsonDict, TaskContext, TaskType


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ----------------------------------------------------------------------- sleep


class SleepParams(_Params):
    seconds: float = Field(default=1.0, ge=0, le=300)


async def _sleep(ctx: TaskContext, params: SleepParams) -> JsonDict:
    await asyncio.sleep(params.seconds)
    return {"slept_s": params.seconds}


SLEEP = TaskType(
    name="sleep",
    params_model=SleepParams,
    handler=_sleep,
    description="Wait for a number of seconds, then succeed.",
)


# ----------------------------------------------------------------------- flaky


class FlakyParams(_Params):
    failure_rate: float = Field(
        default=0.5, ge=0, le=1, description="Chance that an attempt fails."
    )
    fail_first_attempts: int = Field(
        default=0, ge=0, le=100, description="Attempts up to this number always fail."
    )
    seconds: float = Field(default=0.0, ge=0, le=60, description="Work time per attempt.")


class FlakyTaskError(RuntimeError):
    """The simulated failure raised by ``flaky``."""


async def _flaky(ctx: TaskContext, params: FlakyParams) -> JsonDict:
    await asyncio.sleep(params.seconds)
    # Not security sensitive: this simulates unreliable work.
    if ctx.attempt <= params.fail_first_attempts or random.random() < params.failure_rate:
        raise FlakyTaskError(f"simulated failure on attempt {ctx.attempt}")
    return {"succeeded_on_attempt": ctx.attempt}


FLAKY = TaskType(
    name="flaky",
    params_model=FlakyParams,
    handler=_flaky,
    description="Fail at a configurable rate, to demonstrate retries and dead-lettering.",
)


# ----------------------------------------------------------------------- notify


class NotifyParams(_Params):
    channel: str = Field(default="general", min_length=1, max_length=64)
    message: str = Field(
        min_length=1,
        max_length=500,
        description="Text with optional ${name} placeholders filled from `vars`.",
    )
    vars: dict[str, Any] = Field(default_factory=dict)
    hold_s: float = Field(
        default=0.0,
        ge=0,
        le=60,
        description=(
            "Keep working this long after delivering. Widens the window between the "
            "effect and completion, so demos and chaos tests can kill a worker inside it."
        ),
    )


_DELIVER = text(
    """
    INSERT INTO notifications (idempotency_key, run_id, task_id, channel, message)
    VALUES (:key, :run_id, :task_id, :channel, :message)
    ON CONFLICT (idempotency_key) DO NOTHING
    RETURNING id
    """
)


async def _notify(ctx: TaskContext, params: NotifyParams) -> JsonDict:
    # The effect commits in its own transaction, before the task completes, so
    # a crash after this point re-runs the task. The unique idempotency key
    # makes that re-run a no-op instead of a duplicate notification.
    async with ctx.engine.begin() as conn:
        inserted = (
            await conn.execute(
                _DELIVER,
                {
                    "key": ctx.idempotency_key,
                    "run_id": ctx.run_id,
                    "task_id": ctx.task_id,
                    "channel": params.channel,
                    "message": Template(params.message).safe_substitute(
                        {k: "" if v is None else str(v) for k, v in params.vars.items()}
                    ),
                },
            )
        ).scalar_one_or_none()
    await asyncio.sleep(params.hold_s)
    return {"delivered": inserted is not None, "duplicate_suppressed": inserted is None}


NOTIFY = TaskType(
    name="notify",
    params_model=NotifyParams,
    handler=_notify,
    description="Post to the in-app notification feed, exactly once per idempotency key.",
    has_side_effects=True,
)

"""Priority aging, applied in SQL. Mirrors :mod:`quorum.domain.priority`."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

# effective = priority + min(max_bonus, floor(waited / interval)), where
# "waited" counts from not_before (when the task became claimable). Only rows
# whose value actually changes are written, and locked rows are skipped, so
# this never blocks a claimer and never churns rows needlessly.
_AGE = text(
    """
    WITH target AS (
        SELECT id,
               priority + LEAST(
                   :max_bonus,
                   floor(GREATEST(0, EXTRACT(EPOCH FROM now() - not_before)) / :interval_s)
               )::int AS value
        FROM task_instances
        WHERE status = 'ready' AND not_before <= now()
    ),
    stale AS (
        SELECT t.id, target.value
        FROM task_instances t JOIN target ON target.id = t.id
        WHERE t.status = 'ready' AND t.effective_priority <> target.value
        FOR UPDATE OF t SKIP LOCKED
    )
    UPDATE task_instances t SET effective_priority = stale.value
    FROM stale WHERE t.id = stale.id
    """
)


async def apply_aging(conn: AsyncConnection, *, interval_s: float, max_bonus: int) -> int:
    """Recompute effective priorities of waiting tasks; return how many changed."""
    result = await conn.execute(_AGE, {"interval_s": interval_s, "max_bonus": max_bonus})
    return int(result.rowcount)

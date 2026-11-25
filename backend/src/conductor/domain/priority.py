"""Priority aging: waiting raises a task's effective priority, so nothing starves.

``effective = priority + min(max_bonus, floor(waited / interval))``

With ``max_bonus`` at least the full width of the priority range, any task
eventually outranks a freshly enqueued task of any priority, so under
sustained high-priority load a low-priority task waits at most about
``(high - low) * interval`` seconds before it is next in line. The scheduler
applies the same formula in SQL (``store/aging.py``); a test pins the two
implementations together.
"""

from __future__ import annotations

import math

PRIORITY_MIN = -100
PRIORITY_MAX = 100


def aging_bonus(waited_s: float, *, interval_s: float, max_bonus: int) -> int:
    if interval_s <= 0:
        raise ValueError("interval_s must be positive")
    if waited_s <= 0:
        return 0
    return min(max_bonus, math.floor(waited_s / interval_s))


def effective_priority(priority: int, waited_s: float, *, interval_s: float, max_bonus: int) -> int:
    return priority + aging_bonus(waited_s, interval_s=interval_s, max_bonus=max_bonus)


def worst_case_wait_s(low: int, high: int, *, interval_s: float, max_bonus: int) -> float | None:
    """Upper bound on how long ``low`` waits behind a stream of fresh ``high`` tasks.

    Returns None if ``max_bonus`` is too small for ``low`` to ever catch up.
    """
    gap = high - low
    if gap < 0:
        return 0.0
    if max_bonus <= gap:
        return None
    return (gap + 1) * interval_s

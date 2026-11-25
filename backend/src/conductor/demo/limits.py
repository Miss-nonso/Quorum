"""In-memory sliding-window rate limits for the public demo.

Per API process. The demo runs one API instance, so this is exact there; with
several instances the effective limit multiplies by the instance count, which
is an acceptable over-allowance for a demo (and documented). Anything that
must hold globally (concurrent-run and queue-depth caps) is enforced in the
database instead.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass
class SlidingWindowLimiter:
    limit: int
    window_s: float
    clock: Callable[[], float] = time.monotonic
    _hits: dict[str, deque[float]] = field(default_factory=dict)

    def check(self, key: str) -> float:
        """Record a hit for ``key`` if allowed. Returns 0 if allowed, else seconds to wait."""
        now = self.clock()
        hits = self._hits.setdefault(key, deque())
        while hits and now - hits[0] >= self.window_s:
            hits.popleft()
        if len(hits) >= self.limit:
            return self.window_s - (now - hits[0])
        hits.append(now)
        if len(self._hits) > 10_000:  # bound memory under a key-spraying client
            self._evict(now)
        return 0.0

    def _evict(self, now: float) -> None:
        stale = [k for k, h in self._hits.items() if not h or now - h[-1] >= self.window_s]
        for key in stale:
            del self._hits[key]

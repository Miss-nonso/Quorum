from __future__ import annotations

from conductor.demo.limits import SlidingWindowLimiter


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_allows_up_to_the_limit_then_reports_the_wait() -> None:
    clock = FakeClock()
    limiter = SlidingWindowLimiter(limit=3, window_s=60, clock=clock)
    assert [limiter.check("ip") for _ in range(3)] == [0, 0, 0]
    clock.now += 20
    assert limiter.check("ip") == 40  # oldest hit leaves the window in 40 s
    assert limiter.check("other-ip") == 0, "keys are independent"


def test_window_slides() -> None:
    clock = FakeClock()
    limiter = SlidingWindowLimiter(limit=1, window_s=10, clock=clock)
    assert limiter.check("k") == 0
    clock.now += 10
    assert limiter.check("k") == 0


def test_memory_is_bounded_under_key_spraying() -> None:
    clock = FakeClock()
    limiter = SlidingWindowLimiter(limit=1, window_s=1, clock=clock)
    for i in range(10_001):
        limiter.check(f"k{i}")
    clock.now += 2
    limiter.check("fresh")
    assert len(limiter._hits) < 100

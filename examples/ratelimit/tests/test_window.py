import pytest

from ratelimit import SlidingWindowLimiter


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_rejects_bad_config():
    with pytest.raises(ValueError):
        SlidingWindowLimiter(limit=0, window=1)
    with pytest.raises(ValueError):
        SlidingWindowLimiter(limit=1, window=0)


def test_keys_are_independent():
    limiter = SlidingWindowLimiter(limit=1, window=1.0, clock=FakeClock())
    assert limiter.allow("a")
    assert limiter.allow("b")


def test_window_slides():
    clock = FakeClock()
    limiter = SlidingWindowLimiter(limit=2, window=1.0, clock=clock)
    for _ in range(10):
        limiter.allow()
    clock.now = 1.5
    assert limiter.allow()


def test_remaining():
    limiter = SlidingWindowLimiter(limit=5, window=1.0, clock=FakeClock())
    limiter.allow()
    limiter.allow()
    assert limiter.remaining() == 3

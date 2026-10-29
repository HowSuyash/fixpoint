"""Sliding-window rate limiter."""

from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Callable, Hashable


class SlidingWindowLimiter:
    """Allow at most `limit` calls per `window` seconds for each key."""

    def __init__(self, limit: int, window: float, clock: Callable[[], float] = time.monotonic):
        if limit < 1:
            raise ValueError("limit must be at least 1")
        if window <= 0:
            raise ValueError("window must be positive")
        self.limit = limit
        self.window = window
        self.clock = clock
        self._hits: dict[Hashable, deque[float]] = defaultdict(deque)

    def _expire(self, key: Hashable, now: float) -> deque[float]:
        hits = self._hits[key]
        while hits and hits[0] <= now - self.window:
            hits.popleft()
        return hits

    def allow(self, key: Hashable = "default") -> bool:
        now = self.clock()
        hits = self._expire(key, now)
        if len(hits) > self.limit:
            return False
        hits.append(now)
        return True

    def remaining(self, key: Hashable = "default") -> int:
        return max(0, self.limit - len(self._expire(key, self.clock())))

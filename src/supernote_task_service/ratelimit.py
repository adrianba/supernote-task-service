"""In-memory fixed-window rate limiting.

Suitable for a single process / single replica deployment. Used only to throttle
unauthenticated / invalid-key requests, keyed by client IP, so that key guessing
and unauthenticated floods cannot exhaust capacity. Authenticated callers are not
rate limited.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass
class _Window:
    count: int
    reset_at: float


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    remaining: int
    retry_after: int


class RateLimiter:
    """Thread-safe fixed-window rate limiter.

    Each unique key gets ``limit`` requests per ``window_seconds``. Stale
    windows are evicted lazily on access to bound memory use.
    """

    def __init__(self, limit: int, window_seconds: int) -> None:
        self._limit = max(1, limit)
        self._window = max(1, window_seconds)
        self._buckets: dict[str, _Window] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> RateLimitResult:
        now = time.monotonic()
        with self._lock:
            self._evict_expired(now)
            window = self._buckets.get(key)
            if window is None or now >= window.reset_at:
                window = _Window(count=0, reset_at=now + self._window)
                self._buckets[key] = window

            reset_in = max(0, round(window.reset_at - now))
            if window.count >= self._limit:
                return RateLimitResult(
                    allowed=False,
                    remaining=0,
                    retry_after=max(1, reset_in),
                )

            window.count += 1
            return RateLimitResult(
                allowed=True,
                remaining=self._limit - window.count,
                retry_after=0,
            )

    def _evict_expired(self, now: float) -> None:
        if len(self._buckets) < 1024:
            return
        expired = [k for k, w in self._buckets.items() if now >= w.reset_at]
        for k in expired:
            del self._buckets[k]

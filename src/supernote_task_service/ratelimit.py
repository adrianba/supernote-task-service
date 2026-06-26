"""In-memory fixed-window rate limiting.

Suitable for a single process / single replica deployment. Buckets are keyed by
the authenticated caller and the client IP so that a leaked key or a noisy IP
cannot exhaust capacity for everyone.
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
    limit: int
    remaining: int
    retry_after: int
    reset_at: int


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
                    limit=self._limit,
                    remaining=0,
                    retry_after=max(1, reset_in),
                    reset_at=reset_in,
                )

            window.count += 1
            return RateLimitResult(
                allowed=True,
                limit=self._limit,
                remaining=self._limit - window.count,
                retry_after=0,
                reset_at=reset_in,
            )

    def _evict_expired(self, now: float) -> None:
        if len(self._buckets) < 1024:
            return
        expired = [k for k, w in self._buckets.items() if now >= w.reset_at]
        for k in expired:
            del self._buckets[k]

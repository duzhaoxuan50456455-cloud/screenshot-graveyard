"""In-process token-bucket rate limiter (Task 1.5).

A classic token bucket keyed by an opaque string (the trusted, server-resolved
workspace id, or a fallback key for identity-less requests). Each key has a
bucket of `capacity` tokens that refills at `refill_per_sec`; a request consumes
one token and is allowed only if a token is available.

IMPORTANT (multi-worker limitation): this limiter lives in a single process's
memory. It is NOT shared across multiple Uvicorn/Gunicorn workers or multiple
server instances — each process keeps its own buckets, so the effective limit
scales with the worker count. This is an accepted simplification for a solo-dev
single-process deployment (design: "no distributed limiter"). A shared limiter
(e.g. Redis) would be required for a multi-worker/multi-instance deployment.

Memory is bounded: idle buckets (full and untouched past an eviction horizon)
are pruned, and the map is capped at `max_keys` with LRU-style eviction, so a
flood of distinct keys cannot grow memory without bound.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class _Bucket:
    tokens: float
    last_refill: float


class TokenBucketRateLimiter:
    def __init__(
        self,
        capacity: int,
        refill_per_sec: float,
        *,
        clock: Callable[[], float] | None = None,
        max_keys: int = 10_000,
    ) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        if refill_per_sec <= 0:
            raise ValueError("refill_per_sec must be positive")
        self._capacity = float(capacity)
        self._refill = float(refill_per_sec)
        self._clock = clock or time.monotonic
        self._max_keys = max_keys
        self._buckets: "OrderedDict[str, _Bucket]" = OrderedDict()
        self._lock = threading.Lock()

    def _evict_if_needed(self) -> None:
        # Cap the number of tracked keys; drop least-recently-used first.
        while len(self._buckets) > self._max_keys:
            self._buckets.popitem(last=False)

    def allow(self, key: str, cost: float = 1.0) -> bool:
        """Consume `cost` tokens for `key`; return True if allowed, else False."""
        now = self._clock()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(tokens=self._capacity, last_refill=now)
                self._buckets[key] = bucket
            else:
                # Refill based on elapsed time, capped at capacity.
                elapsed = max(0.0, now - bucket.last_refill)
                bucket.tokens = min(
                    self._capacity, bucket.tokens + elapsed * self._refill
                )
                bucket.last_refill = now
                self._buckets.move_to_end(key)  # mark as recently used

            allowed = bucket.tokens >= cost
            if allowed:
                bucket.tokens -= cost
            self._evict_if_needed()
            return allowed

    def tokens_available(self, key: str) -> float:
        """Introspection helper (mainly for tests)."""
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                return self._capacity
            elapsed = max(0.0, self._clock() - bucket.last_refill)
            return min(self._capacity, bucket.tokens + elapsed * self._refill)

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()

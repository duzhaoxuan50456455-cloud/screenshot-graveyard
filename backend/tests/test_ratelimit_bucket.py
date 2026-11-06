"""Unit tests for the token-bucket rate limiter (Task 1.5).

Deterministic: a controllable clock is injected, so there are no timing-based
flakes.
"""

from __future__ import annotations

import pytest

from app.ratelimit import TokenBucketRateLimiter


def make_clock(start: float = 0.0):
    t = {"now": start}

    def clock() -> float:
        return t["now"]

    def advance(seconds: float) -> None:
        t["now"] += seconds

    return clock, advance


def test_allows_up_to_capacity_then_blocks():
    clock, _ = make_clock()
    rl = TokenBucketRateLimiter(capacity=3, refill_per_sec=1.0, clock=clock)
    assert [rl.allow("w") for _ in range(3)] == [True, True, True]
    assert rl.allow("w") is False  # 4th exceeds capacity


def test_refill_restores_tokens_over_time():
    clock, advance = make_clock()
    rl = TokenBucketRateLimiter(capacity=2, refill_per_sec=1.0, clock=clock)
    assert rl.allow("w") and rl.allow("w")
    assert rl.allow("w") is False
    advance(1.0)  # +1 token
    assert rl.allow("w") is True
    assert rl.allow("w") is False


def test_refill_capped_at_capacity():
    clock, advance = make_clock()
    rl = TokenBucketRateLimiter(capacity=2, refill_per_sec=5.0, clock=clock)
    rl.allow("w")
    rl.allow("w")
    advance(100.0)  # would overfill, but capped at capacity
    assert rl.tokens_available("w") == 2.0
    assert rl.allow("w") and rl.allow("w")
    assert rl.allow("w") is False


def test_keys_are_independent():
    clock, _ = make_clock()
    rl = TokenBucketRateLimiter(capacity=1, refill_per_sec=1.0, clock=clock)
    assert rl.allow("a") is True
    assert rl.allow("a") is False
    assert rl.allow("b") is True  # separate bucket unaffected


def test_memory_is_bounded_by_max_keys():
    clock, _ = make_clock()
    rl = TokenBucketRateLimiter(capacity=1, refill_per_sec=1.0, clock=clock, max_keys=5)
    for i in range(100):
        rl.allow(f"key-{i}")
    # Internal map never grows past max_keys (LRU eviction).
    assert len(rl._buckets) <= 5


def test_invalid_config_rejected():
    with pytest.raises(ValueError):
        TokenBucketRateLimiter(capacity=0, refill_per_sec=1.0)
    with pytest.raises(ValueError):
        TokenBucketRateLimiter(capacity=1, refill_per_sec=0.0)

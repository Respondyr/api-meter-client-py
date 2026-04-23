"""Unit tests for the local-fallback token bucket.

The clock is injected so we can advance time deterministically without
sleeping.  Anything that depends on wall-clock time is a test smell —
the bucket uses ``time.monotonic`` by default but accepts a fake.
"""

from __future__ import annotations

from apimeter._bucket import LocalBucket
from apimeter._models import LocalFallbackConfig


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_bucket_grants_up_to_capacity():
    clk = FakeClock()
    b = LocalBucket(LocalFallbackConfig(rate_per_second=1.0, burst=3), clock=clk)
    assert b.take("gbp", "reply") is True
    assert b.take("gbp", "reply") is True
    assert b.take("gbp", "reply") is True
    assert b.take("gbp", "reply") is False  # empty


def test_bucket_refills_over_time():
    clk = FakeClock()
    b = LocalBucket(LocalFallbackConfig(rate_per_second=2.0, burst=1), clock=clk)
    assert b.take("gbp", "reply") is True
    assert b.take("gbp", "reply") is False
    clk.advance(0.5)  # at 2 tok/s, 0.5s = 1 token
    assert b.take("gbp", "reply") is True


def test_bucket_keys_are_scoped_per_endpoint():
    clk = FakeClock()
    b = LocalBucket(LocalFallbackConfig(rate_per_second=1.0, burst=1), clock=clk)
    assert b.take("gbp", "reply") is True
    # A different endpoint starts fresh; draining "reply" must not
    # starve "fetch-reviews".
    assert b.take("gbp", "fetch-reviews") is True


def test_bucket_refill_caps_at_burst():
    clk = FakeClock()
    b = LocalBucket(LocalFallbackConfig(rate_per_second=1.0, burst=2), clock=clk)
    b.take("x", "y")  # burn one
    clk.advance(10_000)  # ten thousand seconds later
    # Should still be capped at 2, not ten thousand tokens available.
    assert b.take("x", "y") is True
    assert b.take("x", "y") is True
    assert b.take("x", "y") is False


def test_bucket_wait_for_when_empty():
    clk = FakeClock()
    b = LocalBucket(LocalFallbackConfig(rate_per_second=2.0, burst=1), clock=clk)
    b.take("x", "y")
    wait = b.wait_for("x", "y")
    # At 2 tok/s we need 0.5s for the next token.
    assert 0.4 <= wait.total_seconds() <= 0.6


def test_bucket_wait_for_zero_when_full():
    clk = FakeClock()
    b = LocalBucket(LocalFallbackConfig(rate_per_second=1.0, burst=1), clock=clk)
    # Never touched — wait_for should be 0, not negative.
    assert b.wait_for("x", "y").total_seconds() == 0.0


def test_bucket_bad_config_falls_back_to_sane_defaults():
    clk = FakeClock()
    b = LocalBucket(LocalFallbackConfig(rate_per_second=0, burst=0), clock=clk)
    # Zero/zero would deadlock — constructor clamps to 1 tok/s burst=5.
    for _ in range(5):
        assert b.take("x", "y") is True
    assert b.take("x", "y") is False

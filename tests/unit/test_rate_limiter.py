"""Unit tests for the async token-bucket RateLimiter."""

from __future__ import annotations

import time

import pytest

from bracket_team.scraper.rate_limiter import RateLimiter


async def test_first_acquire_is_immediate():
    """A freshly created limiter has one token; first acquire should not sleep."""
    limiter = RateLimiter(rate=1.0)
    start = time.monotonic()
    await limiter.acquire()
    elapsed = time.monotonic() - start
    assert elapsed < 0.1, f"First acquire took {elapsed:.3f}s — expected near-zero"


async def test_second_acquire_waits():
    """Second back-to-back acquire should wait ~1 second at rate=1."""
    limiter = RateLimiter(rate=1.0)
    await limiter.acquire()  # consume the initial token
    start = time.monotonic()
    await limiter.acquire()
    elapsed = time.monotonic() - start
    assert 0.8 < elapsed < 1.5, f"Expected ~1s wait, got {elapsed:.3f}s"


async def test_high_rate_allows_burst():
    """rate=10 should allow 10 acquires without meaningful delay."""
    limiter = RateLimiter(rate=10.0)
    start = time.monotonic()
    for _ in range(10):
        await limiter.acquire()
    elapsed = time.monotonic() - start
    assert elapsed < 0.5, f"10 acquires at rate=10 took {elapsed:.3f}s"

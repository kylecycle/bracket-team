"""Async token-bucket rate limiter for scraper HTTP requests."""

from __future__ import annotations

import asyncio
import time


class RateLimiter:
    """Token-bucket rate limiter: allows at most `rate` requests per second."""

    def __init__(self, rate: float) -> None:
        self._rate = rate  # requests per second
        self._tokens = rate
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Block until a token is available, then consume one."""
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_refill
            self._tokens = min(self._rate, self._tokens + elapsed * self._rate)
            self._last_refill = now

            if self._tokens < 1:
                wait = (1 - self._tokens) / self._rate
                await asyncio.sleep(wait)
                self._tokens = 0
            else:
                self._tokens -= 1

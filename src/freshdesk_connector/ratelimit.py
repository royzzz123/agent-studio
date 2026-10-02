"""Client-side rate limiting and retry policy.

Freshdesk enforces a per-account, per-minute API credit quota shared by every
integration on the helpdesk (the trial plan allows 50/min; paid plans more).
Some calls cost more than one credit (each `include=` embed adds one).
The connector defends that quota in three layers:

1. TokenBucket: proactively spaces requests so we stay under a budget,
   by default 50% of the quota the account reports, leaving room for the
   merchant's other integrations. Short bursts (up to ~10s of budget) are allowed.
2. Adaptive throttle: every response reports `X-RateLimit-Total` and
   `X-RateLimit-Remaining`. When remaining credits fall below a reserve, the
   bucket is drained so the next call waits for a fresh token.
3. Retry: a 429 is retried after the server's `Retry-After`; 5xx responses and
   network errors are retried with exponential backoff and full jitter.
   Waits longer than `max_wait` are not slept through. They surface as a
   RateLimitedError, so an agent never hangs silently for minutes.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], float]


class TokenBucket:
    def __init__(self, rate_per_minute: float, capacity: float | None = None, *, clock: Clock = time.monotonic, sleep: Sleep = asyncio.sleep):
        if rate_per_minute <= 0:
            raise ValueError("rate_per_minute must be positive")
        self.rate_per_sec = rate_per_minute / 60.0
        self.capacity = capacity if capacity is not None else max(1.0, min(10.0, rate_per_minute / 6))
        self.tokens = self.capacity
        self._clock = clock
        self._sleep = sleep
        self._last = clock()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = self._clock()
        self.tokens = min(self.capacity, self.tokens + (now - self._last) * self.rate_per_sec)
        self._last = now

    async def acquire(self, cost: float = 1.0) -> float:
        """Wait until `cost` tokens are available, then consume them. Returns seconds waited."""
        cost = min(cost, self.capacity)
        waited = 0.0
        async with self._lock:
            while True:
                self._refill()
                if self.tokens >= cost:
                    self.tokens -= cost
                    return waited
                delay = (cost - self.tokens) / self.rate_per_sec
                await self._sleep(delay)
                waited += delay

    def drain(self) -> None:
        self._refill()
        self.tokens = min(self.tokens, 0.0)

    def set_rate(self, rate_per_minute: float) -> None:
        self._refill()
        self.rate_per_sec = rate_per_minute / 60.0
        self.capacity = max(1.0, min(20.0, rate_per_minute / 6))
        self.tokens = min(self.tokens, self.capacity)


@dataclass
class RetryPolicy:
    max_retries: int = 4
    base_delay: float = 0.5
    max_delay: float = 20.0
    max_wait: float = 65.0  # longest Retry-After we are willing to sleep through
    retry_statuses: frozenset[int] = frozenset({429, 500, 502, 503, 504})
    rng: random.Random = field(default_factory=random.Random)

    def backoff(self, attempt: int) -> float:
        """Exponential backoff with full jitter (attempt is 0-based)."""
        return self.rng.uniform(0, min(self.max_delay, self.base_delay * (2**attempt)))


@dataclass
class QuotaState:
    """Last quota numbers Freshdesk reported. Exposed via the connector_status tool."""

    total: int | None = None
    remaining: int | None = None
    last_cost: int | None = None
    throttled_count: int = 0
    retries: int = 0

    def update(self, headers) -> None:
        def as_int(name: str) -> int | None:
            v = headers.get(name)
            try:
                return int(v) if v is not None else None
            except ValueError:
                return None

        self.total = as_int("x-ratelimit-total") or self.total
        rem = as_int("x-ratelimit-remaining")
        self.remaining = rem if rem is not None else self.remaining
        self.last_cost = as_int("x-ratelimit-used-currentrequest") or self.last_cost


def parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None  # HTTP-date form is not used by Freshdesk

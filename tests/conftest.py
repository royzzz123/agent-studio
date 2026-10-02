from __future__ import annotations

import httpx
import pytest

from freshdesk_connector.client import FreshdeskClient
from freshdesk_connector.config import Credentials
from freshdesk_connector.mock_server import MOCK_API_KEY, MockState, create_app
from freshdesk_connector.ratelimit import RetryPolicy


class FakeClock:
    """Shared virtual clock: sleeping advances time instantly, so rate-limit tests run in milliseconds."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def make_client(clock):
    """Build a FreshdeskClient wired to the in-process mock Freshdesk app."""
    created: list[FreshdeskClient] = []

    def factory(*, rate_limit: int = 1000, fail_next: list[int] | None = None, api_key: str = MOCK_API_KEY, client_rate: float = 6000, max_retries: int = 4):
        state = MockState(rate_limit_per_minute=rate_limit, fail_next=fail_next, clock=clock)
        transport = httpx.ASGITransport(app=create_app(state))
        client = FreshdeskClient(
            Credentials("http://mock.test/api/v2", api_key, "test"),
            rate_per_minute=client_rate,
            retry=RetryPolicy(max_retries=max_retries),
            transport=transport,
            sleep=clock.sleep,
        )
        client.bucket._clock = clock
        client.bucket._last = clock()
        client.mock = state
        created.append(client)
        return client

    yield factory

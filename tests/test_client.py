import pytest

from freshdesk_connector.errors import AuthError, InvalidRequestError, NotFoundError, RateLimitedError, UpstreamError


async def test_list_tickets_paginates_with_link_header(make_client):
    client = make_client()
    p1 = await client.list_tickets(per_page=10)
    assert len(p1) == 10 and p1.has_more
    seen = {t["id"] for t in p1}
    page = 1
    while p1.has_more:
        page += 1
        p1 = await client.list_tickets(per_page=10, page=page)
        seen |= {t["id"] for t in p1}
    assert len(seen) == sum(1 for t in client.mock.data["tickets"] if t["created_at"] >= _cutoff(client))


def _cutoff(client):
    from datetime import timedelta

    from freshdesk_connector.mock_server import _iso

    return _iso(client.mock.data["now"] - timedelta(days=30))


async def test_get_ticket_with_includes_costs_extra_credits(make_client):
    client = make_client()
    t = await client.get_ticket(1001, ["conversations", "requester", "stats"])
    assert t["requester"]["id"] == t["requester_id"]
    assert client.quota.last_cost == 4


async def test_search_reports_total_and_has_more(make_client):
    client = make_client()
    p = await client.search("tickets", "status:2 OR status:3 OR status:4 OR status:5")
    assert p.total == 64 and len(p) == 30 and p.has_more


async def test_search_page_cap_enforced_locally(make_client):
    client = make_client()
    with pytest.raises(InvalidRequestError, match="1-10"):
        await client.search("tickets", "status:2", page=11)
    assert client.mock.request_log == []  # no API credit spent


async def test_not_found_and_bad_request(make_client):
    client = make_client()
    with pytest.raises(NotFoundError):
        await client.get_ticket(999999)
    with pytest.raises(InvalidRequestError) as e:
        await client.search("tickets", "subject:'refund'")
    assert "not a searchable field" in e.value.agent_message()


async def test_bad_api_key(make_client):
    client = make_client(api_key="wrong")
    with pytest.raises(AuthError) as e:
        await client.me()
    assert "wrong" not in e.value.agent_message()


async def test_429_is_retried_after_retry_after(make_client, clock):
    client = make_client(rate_limit=50)
    client.mock.consume(50)  # another integration exhausted the shared quota
    me = await client.me()  # 429 -> sleep Retry-After -> success
    assert me["id"] == 101
    assert client.quota.throttled_count == 1
    assert any(s >= 59 for s in clock.slept)


async def test_429_beyond_max_retries_surfaces_rate_limited(make_client):
    client = make_client(rate_limit=50, max_retries=0)
    client.mock.consume(50)
    with pytest.raises(RateLimitedError) as e:
        await client.me()
    assert e.value.retry_after and "Wait about" in e.value.agent_message()


async def test_5xx_retried_with_backoff_then_succeeds(make_client, clock):
    client = make_client(fail_next=[503, 502])
    assert (await client.me())["id"] == 101
    assert client.quota.retries == 2 and len(clock.slept) >= 2


async def test_5xx_exhausts_retries(make_client):
    client = make_client(fail_next=[500] * 10, max_retries=2)
    with pytest.raises(UpstreamError):
        await client.me()


async def test_adaptive_throttle_slows_down_near_quota(make_client, clock):
    # Mock quota 20/min; client budget is far higher. Once remaining <= 10% the bucket is drained.
    client = make_client(rate_limit=20)
    for _ in range(18):
        await client.me()
    assert client.bucket.rate_per_sec == pytest.approx(20 / 60)  # clamped to the account's quota
    assert client.quota.throttled_count == 0
    assert sum(clock.slept) > 0  # proactively waited instead of hitting 429

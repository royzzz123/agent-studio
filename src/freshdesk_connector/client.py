"""Async Freshdesk API v2 client with auth, pagination, rate limiting and retries."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import httpx

from . import __version__
from .config import Credentials
from .errors import (
    AuthError,
    FreshdeskError,
    InvalidRequestError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitedError,
    UpstreamError,
)
from .ratelimit import QuotaState, RetryPolicy, Sleep, TokenBucket, parse_retry_after

log = logging.getLogger(__name__)

_NEXT_LINK_RE = re.compile(r'<[^>]+>\s*;\s*rel="?next"?')
SEARCH_PAGE_SIZE = 30  # fixed by Freshdesk
SEARCH_MAX_PAGE = 10  # Freshdesk caps search at 10 pages (300 results)
MAX_PER_PAGE = 100


class Page(list):
    """A list of items plus pagination metadata."""

    def __init__(self, items: list[Any], page: int, has_more: bool, total: int | None = None):
        super().__init__(items)
        self.page = page
        self.has_more = has_more
        self.total = total


def _clamp_page(page: int, per_page: int | None = None) -> tuple[int, int | None]:
    if page < 1:
        raise InvalidRequestError("page must be >= 1")
    if per_page is not None and not 1 <= per_page <= MAX_PER_PAGE:
        raise InvalidRequestError(f"per_page must be between 1 and {MAX_PER_PAGE}")
    return page, per_page


class FreshdeskClient:
    def __init__(
        self,
        credentials: Credentials,
        *,
        rate_per_minute: float | None = None,
        quota_share: float = 0.5,
        retry: RetryPolicy | None = None,
        timeout: float = 20.0,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = asyncio.sleep,
    ):
        self.credentials = credentials
        self.retry = retry or RetryPolicy()
        self.quota = QuotaState()
        # Budget: an explicit rate, or else a share of the account quota Freshdesk reports in
        # X-RateLimit-Total (leaving the rest to the merchant's other integrations). Until the
        # first response arrives we assume the smallest plan (trial: 50/min).
        self.configured_rate = rate_per_minute
        self.quota_share = quota_share
        self.bucket = TokenBucket(rate_per_minute or 50 * quota_share, sleep=sleep)
        self._sleep = sleep
        self._http = httpx.AsyncClient(
            base_url=credentials.base_url.rstrip("/") + "/",
            auth=httpx.BasicAuth(credentials.api_key, "X"),  # Freshdesk: API key as username, any password
            timeout=timeout,
            transport=transport,
            headers={"User-Agent": f"freshdesk-connector/{__version__}", "Accept": "application/json"},
            follow_redirects=False,  # never forward the Authorization header to a redirect target
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> FreshdeskClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    # ---- core request loop -------------------------------------------------

    def _observe_quota(self, resp: httpx.Response) -> None:
        self.quota.update(resp.headers)
        total, remaining = self.quota.total, self.quota.remaining
        if total:
            # Never plan to use more than the account's quota, even if configured higher.
            self.bucket.set_rate(min(self.configured_rate or total * self.quota_share, total))
        if remaining is not None and total:
            reserve = max(2, int(total * 0.1))
            if remaining <= reserve:
                log.warning("Freshdesk quota low (%s/%s remaining); throttling", remaining, total)
                self.bucket.drain()

    async def request(self, method: str, path: str, *, params: dict[str, Any] | None = None, json: Any = None, cost: int = 1) -> httpx.Response:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        idempotent = method.upper() in ("GET", "HEAD", "DELETE", "PUT")
        attempt = 0
        while True:
            await self.bucket.acquire(cost)
            try:
                resp = await self._http.request(method, path.lstrip("/"), params=params, json=json)
            except (httpx.TimeoutException, httpx.NetworkError) as e:
                if idempotent and attempt < self.retry.max_retries:
                    await self._backoff(attempt, f"{type(e).__name__}")
                    attempt += 1
                    continue
                raise UpstreamError(f"Could not reach Freshdesk ({type(e).__name__})") from e

            self._observe_quota(resp)
            if resp.status_code < 400:
                return resp

            if resp.status_code == 429:
                self.quota.throttled_count += 1
                retry_after = parse_retry_after(resp.headers.get("retry-after")) or 60.0
                self.bucket.drain()
                # A 429 means the request was rejected, so retrying is safe even for POST.
                if attempt < self.retry.max_retries and retry_after <= self.retry.max_wait:
                    self.quota.retries += 1
                    log.warning("Freshdesk 429; retrying in %.1fs", retry_after)
                    await self._sleep(retry_after + self.retry.rng.uniform(0, 0.5))
                    attempt += 1
                    continue
                raise RateLimitedError("API rate limit exceeded", retry_after=retry_after)

            if resp.status_code in self.retry.retry_statuses and idempotent and attempt < self.retry.max_retries:
                await self._backoff(attempt, f"HTTP {resp.status_code}")
                attempt += 1
                continue

            raise self._to_error(resp)

    async def _backoff(self, attempt: int, reason: str) -> None:
        delay = self.retry.backoff(attempt)
        self.quota.retries += 1
        log.warning("Freshdesk request failed (%s); retry %d in %.2fs", reason, attempt + 1, delay)
        await self._sleep(delay)

    @staticmethod
    def _to_error(resp: httpx.Response) -> FreshdeskError:
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        msg = body.get("message") or body.get("description") or resp.reason_phrase or "request failed"
        status = resp.status_code
        if status == 401:
            return AuthError("Authentication failed", status=status)
        if status == 403:
            return PermissionDeniedError(msg, status=status)
        if status == 404:
            return NotFoundError("Resource not found", status=status)
        if status in (400, 409, 422):
            return InvalidRequestError(msg, status=status, details=body.get("errors"))
        if status >= 500:
            return UpstreamError(msg, status=status)
        return FreshdeskError(msg, status=status)

    async def get(self, path: str, params: dict[str, Any] | None = None, cost: int = 1) -> Any:
        resp = await self.request("GET", path, params=params, cost=cost)
        return resp.json()

    async def get_page(self, path: str, params: dict[str, Any], page: int, cost: int = 1) -> Page:
        resp = await self.request("GET", path, params={**params, "page": page}, cost=cost)
        data = resp.json()
        has_more = bool(_NEXT_LINK_RE.search(resp.headers.get("link", "")))
        return Page(data, page, has_more)

    # ---- resources -----------------------------------------------------------

    async def me(self) -> dict[str, Any]:
        return await self.get("agents/me")

    async def list_tickets(
        self,
        *,
        filter: str | None = None,
        requester_id: int | None = None,
        updated_since: str | None = None,
        order_by: str = "updated_at",
        order_type: str = "desc",
        page: int = 1,
        per_page: int = 30,
        include: list[str] | None = None,
    ) -> Page:
        _clamp_page(page, per_page)
        include = include or []
        params = {
            "filter": filter,
            "requester_id": requester_id,
            "updated_since": updated_since,
            "order_by": order_by,
            "order_type": order_type,
            "per_page": per_page,
            "include": ",".join(include) or None,
        }
        return await self.get_page("tickets", params, page, cost=1 + len(include))

    async def get_ticket(self, ticket_id: int, include: list[str] | None = None) -> dict[str, Any]:
        include = include or []
        return await self.get(f"tickets/{int(ticket_id)}", {"include": ",".join(include) or None}, cost=1 + len(include))

    async def list_conversations(self, ticket_id: int, page: int = 1, per_page: int = 30) -> Page:
        _clamp_page(page, per_page)
        return await self.get_page(f"tickets/{int(ticket_id)}/conversations", {"per_page": per_page}, page)

    async def search(self, entity: str, query: str, page: int = 1) -> Page:
        if entity not in ("tickets", "contacts", "companies"):
            raise InvalidRequestError(f"Cannot search {entity}")
        if not 1 <= page <= SEARCH_MAX_PAGE:
            raise InvalidRequestError(f"Search page must be 1-{SEARCH_MAX_PAGE} (Freshdesk returns at most 300 results); narrow the query instead")
        data = await self.get(f"search/{entity}", {"query": f'"{query}"', "page": page})
        results, total = data.get("results", []), data.get("total", 0)
        return Page(results, page, has_more=page * SEARCH_PAGE_SIZE < min(total, SEARCH_PAGE_SIZE * SEARCH_MAX_PAGE), total=total)

    async def list_contacts(self, *, email: str | None = None, phone: str | None = None, mobile: str | None = None, company_id: int | None = None, state: str | None = None, page: int = 1, per_page: int = 30) -> Page:
        _clamp_page(page, per_page)
        params = {"email": email, "phone": phone, "mobile": mobile, "company_id": company_id, "state": state, "per_page": per_page}
        return await self.get_page("contacts", params, page)

    async def autocomplete_contacts(self, term: str) -> list[dict[str, Any]]:
        return await self.get("contacts/autocomplete", {"term": term})

    async def get_contact(self, contact_id: int) -> dict[str, Any]:
        return await self.get(f"contacts/{int(contact_id)}")

    async def list_companies(self, page: int = 1, per_page: int = 30) -> Page:
        _clamp_page(page, per_page)
        return await self.get_page("companies", {"per_page": per_page}, page)

    async def get_company(self, company_id: int) -> dict[str, Any]:
        return await self.get(f"companies/{int(company_id)}")

    async def list_agents(self, page: int = 1, per_page: int = 50) -> Page:
        _clamp_page(page, per_page)
        return await self.get_page("agents", {"per_page": per_page}, page)

    async def list_groups(self, page: int = 1, per_page: int = 50) -> Page:
        _clamp_page(page, per_page)
        return await self.get_page("groups", {"per_page": per_page}, page)

    async def ticket_fields(self) -> list[dict[str, Any]]:
        return await self.get("ticket_fields")

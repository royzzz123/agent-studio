"""MCP server exposing read-only Freshdesk tools to an Agent Studio agent."""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from functools import wraps
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

from . import __version__
from .client import FreshdeskClient
from .config import load_credentials
from .errors import FreshdeskError
from .query import build_ticket_query, finalize
from .shaping import Shaper

INSTRUCTIONS = """\
Read-only access to the merchant's Freshdesk helpdesk (tickets, conversations, contacts, companies).

How to work efficiently (every call spends the merchant's shared Freshdesk API quota):
- Structured questions (status, priority, tag, agent, group, dates): use search_tickets.
- Keyword questions ("tickets about refunds"): Freshdesk has no free-text search API; use
  find_tickets_by_text, which scans recently updated tickets client-side.
- To answer about one ticket, call get_ticket (includes the latest 10 conversations), then
  list_ticket_conversations only if you need the full thread.
- Resolve people/teams to IDs with list_agents, list_groups, find_contacts before filtering by ID.
- Status/priority/source are returned as labels ("open", "urgent"). Timestamps are UTC ISO-8601.
- If a tool reports a rate limit, stop calling tools and tell the user how long to wait.
This connector cannot create, update, reply to, assign, or delete anything.
"""

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True)

ClientProvider = Callable[[], Awaitable[FreshdeskClient]]


def default_client_provider() -> ClientProvider:
    client: FreshdeskClient | None = None

    async def provide() -> FreshdeskClient:
        nonlocal client
        if client is None:
            rate = os.environ.get("FRESHDESK_RATE_LIMIT_PER_MINUTE")
            share = float(os.environ.get("FRESHDESK_QUOTA_SHARE", "0.5"))
            client = FreshdeskClient(load_credentials(), rate_per_minute=float(rate) if rate else None, quota_share=share)
        return client

    return provide


def _page_meta(page, items: list[Any], **extra: Any) -> dict[str, Any]:
    out = {"page": page.page, "count": len(items), "has_more": page.has_more}
    if page.total is not None:
        out["total"] = page.total
    if page.has_more:
        out["next_page"] = page.page + 1
    return {**out, **extra}


def build_server(client_provider: ClientProvider | None = None, *, redact_pii: bool | None = None) -> MCPServer:
    provide = client_provider or default_client_provider()
    if redact_pii is None:
        redact_pii = os.environ.get("FRESHDESK_REDACT_PII", "").lower() in ("1", "true", "yes")
    shaper = Shaper(redact_pii=redact_pii)
    mcp = MCPServer("freshdesk", title="Freshdesk (read-only)", version=__version__, instructions=INSTRUCTIONS)

    def tool(fn):
        """Register a read-only tool and translate connector errors into agent-readable tool errors."""

        @wraps(fn)
        async def wrapper(*args, **kwargs):
            try:
                return await fn(*args, **kwargs)
            except FreshdeskError as e:
                raise ToolError(e.agent_message()) from e

        mcp.tool(annotations=READ_ONLY, structured_output=True)(wrapper)
        return fn

    # ---- tickets ---------------------------------------------------------------

    @tool
    async def list_tickets(
        view: Annotated[Literal["recent", "new_and_my_open", "watching", "spam", "deleted"], Field(description="'recent' = all tickets (created in the last 30 days unless updated_since is set). Other values are Freshdesk's predefined views, relative to the API key's agent.")] = "recent",
        updated_since: Annotated[str | None, Field(description="ISO date/time; only tickets updated on/after this (also lifts the 30-day window).")] = None,
        order_by: Literal["created_at", "updated_at", "due_by", "status"] = "updated_at",
        order_type: Literal["asc", "desc"] = "desc",
        page: Annotated[int, Field(ge=1)] = 1,
        per_page: Annotated[int, Field(ge=1, le=100)] = 30,
        include_requester: Annotated[bool, Field(description="Embed requester name/email (costs 1 extra API credit).")] = False,
    ) -> dict[str, Any]:
        """List tickets newest-first with pagination. Use search_tickets when filtering by status, priority, tag, agent or dates."""
        client = await provide()
        p = await client.list_tickets(
            filter=None if view == "recent" else view,
            updated_since=updated_since,
            order_by=order_by,
            order_type=order_type,
            page=page,
            per_page=per_page,
            include=["requester"] if include_requester else None,
        )
        tickets = [shaper.ticket(t) for t in p]
        return {"tickets": tickets, **_page_meta(p, tickets)}

    @tool
    async def get_ticket(
        ticket_id: Annotated[int, Field(ge=1, description="Freshdesk ticket ID (the number shown as #1234).")],
        include_conversations: Annotated[bool, Field(description="Embed up to 10 most recent replies/notes.")] = True,
        include_requester: bool = True,
    ) -> dict[str, Any]:
        """Get one ticket in full: description, custom fields, requester, SLA/response stats, and recent conversations."""
        include = ["stats"]
        if include_conversations:
            include.append("conversations")
        if include_requester:
            include.append("requester")
        client = await provide()
        return {"ticket": shaper.ticket(await client.get_ticket(ticket_id, include), detail=True)}

    @tool
    async def list_ticket_conversations(
        ticket_id: Annotated[int, Field(ge=1)],
        page: Annotated[int, Field(ge=1)] = 1,
        per_page: Annotated[int, Field(ge=1, le=100)] = 30,
    ) -> dict[str, Any]:
        """List the full reply/note thread of a ticket, oldest first. 'author' is customer or agent; private notes are internal-only."""
        client = await provide()
        p = await client.list_conversations(ticket_id, page, per_page)
        convs = [shaper.conversation(c) for c in p]
        return {"ticket_id": ticket_id, "conversations": convs, **_page_meta(p, convs)}

    @tool
    async def search_tickets(
        status: Annotated[list[str] | None, Field(description="Any of: open, pending, resolved, closed (or numeric custom status codes). Combined with OR.")] = None,
        priority: Annotated[list[str] | None, Field(description="Any of: low, medium, high, urgent. Combined with OR.")] = None,
        tags: Annotated[list[str] | None, Field(description="Ticket has any of these tags.")] = None,
        ticket_type: Annotated[str | None, Field(description="Exact ticket type, e.g. 'Refund'. See describe_ticket_fields for valid types.")] = None,
        agent_id: Annotated[int | None, Field(description="Assigned agent ID (see list_agents).")] = None,
        unassigned: Annotated[bool, Field(description="Only tickets with no agent assigned.")] = False,
        group_id: Annotated[int | None, Field(description="Assigned group ID (see list_groups).")] = None,
        created_after: Annotated[str | None, Field(description="YYYY-MM-DD, inclusive.")] = None,
        created_before: Annotated[str | None, Field(description="YYYY-MM-DD, inclusive.")] = None,
        updated_after: str | None = None,
        updated_before: str | None = None,
        due_before: Annotated[str | None, Field(description="Resolution due on/before YYYY-MM-DD (find SLA risks).")] = None,
        custom_fields: Annotated[dict[str, str | int | bool] | None, Field(description="Custom field filters, e.g. {'order_id': 'ORD-1001'}; names from describe_ticket_fields.")] = None,
        raw_query: Annotated[str | None, Field(description="Advanced: a Freshdesk filter query, e.g. \"priority:4 AND (status:2 OR status:3)\". Overrides the other filters.")] = None,
        page: Annotated[int, Field(ge=1, le=10, description="30 results per page; Freshdesk returns at most 10 pages.")] = 1,
    ) -> dict[str, Any]:
        """Search tickets by structured filters (all filters are ANDed). Not for free-text keywords; use find_tickets_by_text for that.
        Results can lag a few minutes behind very recent changes (search index delay)."""
        if raw_query:
            query = finalize(raw_query)
        else:
            query = build_ticket_query(
                status=status, priority=priority, tags=tags, ticket_type=ticket_type, agent_id=agent_id,
                unassigned=unassigned, group_id=group_id, created_after=created_after, created_before=created_before,
                updated_after=updated_after, updated_before=updated_before, due_before=due_before, custom_fields=custom_fields,
            )
        client = await provide()
        p = await client.search("tickets", query, page)
        tickets = [shaper.ticket(t) for t in p]
        meta = _page_meta(p, tickets, query=query)
        if p.total and p.total > 300:
            meta["warning"] = f"{p.total} matches but Freshdesk only returns the first 300; add filters to narrow the search."
        return {"tickets": tickets, **meta}

    @tool
    async def find_tickets_by_text(
        text: Annotated[str, Field(min_length=2, max_length=100, description="Words to look for (case-insensitive; all words must appear).")],
        updated_within_days: Annotated[int, Field(ge=1, le=90)] = 30,
        max_tickets_scanned: Annotated[int, Field(ge=100, le=500, description="Upper bound on tickets scanned (100 per API call).")] = 300,
        search_description: Annotated[bool, Field(description="Also match the ticket description (costs 1 extra credit per 100 tickets).")] = False,
    ) -> dict[str, Any]:
        """Keyword search over ticket subjects (optionally descriptions) of recently updated tickets.
        Freshdesk's API has no free-text ticket search, so this scans the most recently updated tickets client-side; older tickets are not covered."""
        words = [w for w in text.lower().split() if w]
        since = (datetime.now(UTC) - timedelta(days=updated_within_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        include = ["description"] if search_description else None
        client = await provide()
        matches, scanned, page, exhausted = [], 0, 1, False
        while scanned < max_tickets_scanned:
            p = await client.list_tickets(updated_since=since, page=page, per_page=100, include=include)
            for t in p:
                hay = t.get("subject") or ""
                if search_description:
                    hay += " " + (t.get("description_text") or "")
                if all(w in hay.lower() for w in words):
                    shaped = shaper.ticket(t)
                    shaped.pop("description", None)
                    matches.append(shaped)
            scanned += len(p)
            if not p.has_more:
                exhausted = True
                break
            page += 1
        return {
            "tickets": matches,
            "count": len(matches),
            "tickets_scanned": scanned,
            "coverage": f"all tickets updated in the last {updated_within_days} days" if exhausted else f"only the {scanned} most recently updated tickets (more exist; raise max_tickets_scanned or narrow updated_within_days)",
        }

    @tool
    async def describe_ticket_fields() -> dict[str, Any]:
        """List ticket fields with their allowed values: statuses (incl. custom), priorities, ticket types, and custom fields (names usable in search_tickets custom_fields)."""
        client = await provide()
        fields = await client.ticket_fields()
        out = []
        for f in fields:
            item: dict[str, Any] = {"name": f.get("name"), "label": f.get("label"), "type": f.get("type"), "custom": not f.get("default", False)}
            choices = f.get("choices")
            if f.get("name") == "status" and isinstance(choices, dict):
                labels = {int(code): (v[0] if isinstance(v, list) else v) for code, v in choices.items()}
                shaper.status_labels.update({c: str(l).lower().replace(" ", "_") for c, l in labels.items()})
                item["choices"] = {str(c): l for c, l in labels.items()}
            elif choices:
                item["choices"] = choices if isinstance(choices, (list, dict)) else str(choices)
            out.append(item)
        return {"fields": out}

    # ---- contacts & companies -------------------------------------------------

    @tool
    async def find_contacts(
        email: Annotated[str | None, Field(description="Exact email address.")] = None,
        phone: Annotated[str | None, Field(description="Exact phone or mobile number as stored.")] = None,
        name: Annotated[str | None, Field(min_length=2, description="Name prefix (autocomplete); returns IDs and names only.")] = None,
        company_id: int | None = None,
        page: Annotated[int, Field(ge=1)] = 1,
    ) -> dict[str, Any]:
        """Find customers (contacts) by email, phone, name prefix, or company. Provide exactly one criterion."""
        given = [k for k, v in {"email": email, "phone": phone, "name": name, "company_id": company_id}.items() if v is not None]
        if len(given) != 1:
            raise ToolError("Provide exactly one of email, phone, name, company_id.")
        client = await provide()
        if name:
            refs = await client.autocomplete_contacts(name)
            return {"contacts": [{"id": r.get("id"), "name": r.get("name")} for r in refs], "count": len(refs), "note": "Use get_contact for details."}
        if phone:
            p = await client.list_contacts(phone=phone, page=page)
            if not p:
                p = await client.list_contacts(mobile=phone, page=page)
        else:
            p = await client.list_contacts(email=email, company_id=company_id, page=page)
        contacts = [shaper.contact(c) for c in p]
        return {"contacts": contacts, **_page_meta(p, contacts)}

    @tool
    async def get_contact(
        contact_id: Annotated[int, Field(ge=1)],
        include_recent_tickets: Annotated[bool, Field(description="Also return this contact's 10 most recent tickets (1 extra call).")] = True,
    ) -> dict[str, Any]:
        """Get a customer (contact) profile, optionally with their recent tickets."""
        client = await provide()
        out: dict[str, Any] = {"contact": shaper.contact(await client.get_contact(contact_id))}
        if include_recent_tickets:
            p = await client.list_tickets(requester_id=contact_id, per_page=10)
            out["recent_tickets"] = [shaper.ticket(t) for t in p]
        return out

    @tool
    async def list_companies(page: Annotated[int, Field(ge=1)] = 1, per_page: Annotated[int, Field(ge=1, le=100)] = 30) -> dict[str, Any]:
        """List customer companies (B2B accounts) in the helpdesk."""
        client = await provide()
        p = await client.list_companies(page, per_page)
        companies = [shaper.company(c) for c in p]
        return {"companies": companies, **_page_meta(p, companies)}

    @tool
    async def get_company(company_id: Annotated[int, Field(ge=1)]) -> dict[str, Any]:
        """Get one company's profile."""
        client = await provide()
        return {"company": shaper.company(await client.get_company(company_id))}

    # ---- helpdesk directory -----------------------------------------------------

    @tool
    async def list_agents(page: Annotated[int, Field(ge=1)] = 1) -> dict[str, Any]:
        """List support agents (id, name, email). Use to resolve 'assigned to Priya' into an agent_id."""
        client = await provide()
        p = await client.list_agents(page)
        agents = [shaper.agent(a) for a in p]
        return {"agents": agents, **_page_meta(p, agents)}

    @tool
    async def list_groups(page: Annotated[int, Field(ge=1)] = 1) -> dict[str, Any]:
        """List agent groups/teams (id, name). Use to resolve 'the Payments team' into a group_id."""
        client = await provide()
        p = await client.list_groups(page)
        groups = [shaper.group(g) for g in p]
        return {"groups": groups, **_page_meta(p, groups)}

    @tool
    async def connector_status() -> dict[str, Any]:
        """Check connectivity and the remaining Freshdesk API quota. Call this if other tools fail unexpectedly."""
        client = await provide()
        me = await client.me()
        q = client.quota
        return {
            "connected": True,
            "helpdesk": client.credentials.base_url.split("/api/")[0],
            "authenticated_as": shaper.agent(me).get("name"),
            "quota": {"per_minute": q.total, "remaining": q.remaining, "throttled_responses": q.throttled_count, "retries": q.retries},
            "client_budget_per_minute": round(client.bucket.rate_per_sec * 60, 1),
            "pii_redaction": shaper.redact_pii,
        }

    return mcp

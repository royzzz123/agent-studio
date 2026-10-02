"""A local stand-in for the Freshdesk v2 API, loaded with fictional data.

It lets reviewers run the connector, tests and demo without a Freshdesk account,
and it reproduces the behaviours the connector must handle: Basic auth,
Link-header pagination, the 30-day default window on /tickets, the search query
language (subset), the 30x10 search cap, error bodies, and per-minute rate
limiting with Retry-After. All people, emails and phone numbers are made up;
emails use the reserved example.com/.test domains.

Run: freshdesk-connector mock --port 8765
"""

from __future__ import annotations

import base64
import random
import re
import time
from collections import deque
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

MOCK_API_KEY = "mock-api-key"

# ---- fictional dataset --------------------------------------------------------

AGENTS = [(101, "Priya Raman", [11]), (102, "Arjun Mehta", [11, 12]), (103, "Sana Qureshi", [12]), (104, "Kabir Das", [13]), (105, "Meera Iyer", [13])]
GROUPS = [(11, "Payments & Refunds"), (12, "Orders & Delivery"), (13, "Subscriptions")]
COMPANIES = [(201, "Chai Point Retail (fictional)", ["chaipoint-demo.test"]), (202, "Kiran Organics (fictional)", ["kiranorganics.test"]), (203, "UrbanFit Studios (fictional)", ["urbanfit.test"])]
FIRST = ["Aarav", "Diya", "Rohan", "Ananya", "Vikram", "Isha", "Neel", "Tara", "Kunal", "Zoya", "Dev", "Riya", "Farhan", "Leela", "Omkar", "Naina"]
LAST = ["Sharma", "Patel", "Nair", "Gupta", "Reddy", "Khan", "Bose", "Menon", "Joshi", "Kapoor"]

SCENARIOS = [
    ("Payment deducted but order not confirmed", "Money was debited from my account via UPI but the order still shows payment pending. Order ref {order}. Please help.", ["payment-failure", "upi"], "Problem", 11),
    ("Refund not received after 7 days", "I cancelled order {order} last week and was told the refund was initiated, but I have not received it yet.", ["refund"], "Refund", 11),
    ("Autopay mandate failed for monthly plan", "My subscription autopay failed this month and now my plan is paused. I did not change my bank. Ref {order}.", ["autopay", "subscription"], "Problem", 13),
    ("Charged twice for the same order", "I see two debits of the same amount for order {order}. Please refund the duplicate charge.", ["payment-failure", "duplicate-charge", "refund"], "Refund", 11),
    ("Where is my order?", "Order {order} was supposed to be delivered three days ago. Tracking has not updated.", ["delivery"], "Question", 12),
    ("Card declined at checkout", "My card keeps getting declined at checkout though it works elsewhere. Tried 3 times for {order}.", ["payment-failure", "card"], "Problem", 11),
    ("Want to cancel subscription", "Please cancel my subscription and stop future autopay debits. Customer since 2024.", ["subscription", "cancellation"], "Question", 13),
    ("Wrong item delivered", "I received a different product than what I ordered in {order}. Need a replacement.", ["delivery", "replacement"], "Incident", 12),
    ("Invoice with GST details needed", "Please share a GST invoice for order {order} for my company records.", ["invoice"], "Question", 12),
    ("Refund credited to wrong account?", "The refund for {order} shows processed but I can't find it in my bank statement. Please share the UTR / ARN.", ["refund", "utr"], "Refund", 11),
]


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def build_dataset(now: datetime | None = None, seed: int = 7, n_tickets: int = 64) -> dict[str, Any]:
    rng = random.Random(seed)
    now = now or datetime.now(UTC).replace(microsecond=0)
    agents = [{"id": i, "available": rng.random() > 0.3, "group_ids": g, "contact": {"name": n, "email": f"{n.split()[0].lower()}@support.example.com"}} for i, n, g in AGENTS]
    groups = [{"id": i, "name": n, "description": f"{n} queue", "agent_ids": [a for a, _, gs in AGENTS if i in gs]} for i, n in GROUPS]
    companies = [{"id": i, "name": n, "domains": d, "description": f"{n}: fictional merchant used for demos", "industry": "Retail", "account_tier": rng.choice(["Basic", "Premium"]), "created_at": _iso(now - timedelta(days=400))} for i, n, d in COMPANIES]
    contacts = []
    for i in range(24):
        first, last = FIRST[i % len(FIRST)], LAST[(i * 3) % len(LAST)]
        contacts.append({
            "id": 5000 + i, "name": f"{first} {last}", "email": f"{first.lower()}.{last.lower()}{i}@example.com",
            "phone": f"+91 90000 {10000 + i:05d}", "mobile": None, "company_id": companies[i % 3]["id"] if i % 4 == 0 else None,
            "job_title": None, "active": True, "tags": ["vip"] if i % 7 == 0 else [], "created_at": _iso(now - timedelta(days=200 - i)), "updated_at": _iso(now - timedelta(days=i)),
        })
    tickets, conversations = [], {}
    conv_id = 900000
    for n in range(n_tickets):
        tid = 1001 + n
        subject, body, tags, ttype, group = SCENARIOS[n % len(SCENARIOS)]
        contact = contacts[rng.randrange(len(contacts))]
        order = f"ORD-{20000 + n * 7}"
        created = now - timedelta(days=rng.uniform(0, 45), minutes=rng.randrange(600))
        updated = min(now, created + timedelta(hours=rng.uniform(0, 96)))
        status = rng.choices([2, 3, 4, 5], weights=[4, 2, 2, 2])[0]
        priority = 4 if "duplicate-charge" in tags else rng.choices([1, 2, 3, 4], weights=[2, 4, 3, 1])[0]
        responder = rng.choice([None] + [a for a, _, gs in AGENTS if group in gs])
        tickets.append({
            "id": tid, "subject": f"{subject} - {order}", "description_text": body.format(order=order), "description": f"<div>{body.format(order=order)}</div>",
            "status": status, "priority": priority, "source": rng.choice([1, 2, 3, 7, 13]), "type": ttype, "tags": list(tags),
            "requester_id": contact["id"], "responder_id": responder, "group_id": group, "company_id": contact["company_id"],
            "created_at": _iso(created), "updated_at": _iso(updated), "due_by": _iso(created + timedelta(days=2)), "fr_due_by": _iso(created + timedelta(hours=8)),
            "is_escalated": status in (2, 3) and created < now - timedelta(days=2), "fr_escalated": False, "spam": False,
            "custom_fields": {"cf_order_id": order, "cf_payment_method": rng.choice(["UPI", "Card", "Netbanking", "Wallet"])}, "attachments": [], "cc_emails": [],
        })
        thread = [{"id": (conv_id := conv_id + 1), "incoming": False, "private": False, "user_id": responder or 101, "created_at": _iso(created + timedelta(hours=2)), "body_text": "Thanks for reaching out. We're checking this with our payments partner and will update you shortly.", "attachments": []}]
        for k in range(rng.randrange(0, 14 if n % 9 == 0 else 3)):
            incoming = k % 2 == 0
            thread.append({"id": (conv_id := conv_id + 1), "incoming": incoming, "private": not incoming and k % 4 == 3, "user_id": contact["id"] if incoming else (responder or 101), "created_at": _iso(created + timedelta(hours=3 + k * 5)), "body_text": "Any update on this? It's been a while." if incoming else "Internal: checked gateway logs, payment captured; escalating to finance.", "attachments": []})
        conversations[tid] = thread
    return {"agents": agents, "groups": groups, "companies": companies, "contacts": contacts, "tickets": tickets, "conversations": conversations, "now": now}


# ---- search query subset parser ------------------------------------------------

_TOKEN_RE = re.compile(r"\s*(\(|\)|AND\b|OR\b|[a-z_][a-z0-9_]*:[<>]?(?:'[^']*'|[^\s()]+))")


class QueryError(ValueError):
    pass


def _tokenize(q: str) -> list[str]:
    pos, out = 0, []
    while pos < len(q):
        m = _TOKEN_RE.match(q, pos)
        if not m:
            if q[pos:].strip() == "":
                break
            raise QueryError(f"Unexpected input near: {q[pos:pos + 20]!r}")
        out.append(m.group(1))
        pos = m.end()
    return out


def _parse(tokens: list[str]):
    def expr(i):
        node, i = term(i)
        while i < len(tokens) and tokens[i] == "OR":
            rhs, i = term(i + 1)
            node = ("or", node, rhs)
        return node, i

    def term(i):
        node, i = atom(i)
        while i < len(tokens) and tokens[i] == "AND":
            rhs, i = atom(i + 1)
            node = ("and", node, rhs)
        return node, i

    def atom(i):
        if i >= len(tokens):
            raise QueryError("Unexpected end of query")
        if tokens[i] == "(":
            node, i = expr(i + 1)
            if i >= len(tokens) or tokens[i] != ")":
                raise QueryError("Missing )")
            return node, i + 1
        field, _, rest = tokens[i].partition(":")
        op = rest[0] if rest[:1] in ("<", ">") else "="
        val = rest[1:] if op != "=" else rest
        return ("cmp", field, op, val.strip("'")), i + 1

    node, i = expr(0)
    if i != len(tokens):
        raise QueryError(f"Unexpected token {tokens[i]!r}")
    return node


_TICKET_FIELDS = {"status", "priority", "type", "tag", "agent_id", "group_id", "requester_id", "created_at", "updated_at", "due_by", "fr_due_by"}


def _matches(node, rec: dict[str, Any]) -> bool:
    kind = node[0]
    if kind == "and":
        return _matches(node[1], rec) and _matches(node[2], rec)
    if kind == "or":
        return _matches(node[1], rec) or _matches(node[2], rec)
    _, field, op, val = node
    if field == "tag":
        return val in rec.get("tags", [])
    if field.startswith("cf_"):
        actual = rec.get("custom_fields", {}).get(field)
    else:
        actual = rec.get({"agent_id": "responder_id"}.get(field, field))
    if field in ("created_at", "updated_at", "due_by", "fr_due_by"):
        day = (actual or "")[:10]
        return day >= val if op == ">" else day <= val if op == "<" else day == val
    if val == "null":
        return actual is None
    if op in ("<", ">"):
        try:
            return float(actual) > float(val) if op == ">" else float(actual) < float(val)
        except (TypeError, ValueError):
            return False
    return str(actual).lower() == val.lower()


def _validate_fields(node, allowed: set[str]) -> None:
    if node[0] in ("and", "or"):
        _validate_fields(node[1], allowed)
        _validate_fields(node[2], allowed)
    elif node[1] not in allowed and not node[1].startswith("cf_"):
        raise QueryError(f"'{node[1]}' is not a searchable field")


# ---- app ------------------------------------------------------------------------


class MockState:
    def __init__(self, rate_limit_per_minute: int = 50, fail_next: list[int] | None = None, clock=time.monotonic):
        self.data = build_dataset()
        self.limit = rate_limit_per_minute
        self.calls: deque[float] = deque()
        self.fail_next = list(fail_next or [])  # status codes to return for the next N requests (fault injection)
        self.clock = clock
        self.request_log: list[str] = []

    def consume(self, n: int) -> None:
        """Simulate other integrations on the same helpdesk spending n credits of the shared quota."""
        self.calls.extend([self.clock()] * n)


def _err(status: int, body: dict[str, Any], headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(body, status_code=status, headers=headers)


def create_app(state: MockState | None = None) -> Starlette:
    st = state or MockState()
    d = st.data

    def guard(request: Request, cost: int = 1) -> JSONResponse | dict[str, str]:
        st.request_log.append(f"{request.method} {request.url.path}?{request.url.query}")
        auth = request.headers.get("authorization", "")
        try:
            user = base64.b64decode(auth.removeprefix("Basic ")).decode().split(":", 1)[0] if auth.startswith("Basic ") else ""
        except Exception:
            user = ""
        if user != MOCK_API_KEY:
            return _err(401, {"code": "invalid_credentials", "message": "You have to be logged in to perform this action."})
        now = st.clock()
        while st.calls and now - st.calls[0] >= 60:
            st.calls.popleft()
        if len(st.calls) + cost > st.limit:
            retry = max(1, int(60 - (now - st.calls[0]))) if st.calls else 60
            return _err(429, {"message": "You have exceeded the limit of requests per minute"}, {"Retry-After": str(retry), "X-RateLimit-Total": str(st.limit), "X-RateLimit-Remaining": "0"})
        st.calls.extend([now] * cost)
        if st.fail_next:
            code = st.fail_next.pop(0)
            return _err(code, {"message": "Injected failure"})
        return {"X-RateLimit-Total": str(st.limit), "X-RateLimit-Remaining": str(st.limit - len(st.calls)), "X-RateLimit-Used-CurrentRequest": str(cost)}

    def includes(request: Request) -> list[str]:
        return [x for x in request.query_params.get("include", "").split(",") if x]

    def paginate(request: Request, items: list[Any], headers: dict[str, str], default_per_page: int = 30) -> JSONResponse:
        try:
            page = int(request.query_params.get("page", 1))
            per_page = int(request.query_params.get("per_page", default_per_page))
        except ValueError:
            return _err(400, {"description": "Validation failed", "errors": [{"field": "page", "message": "It should be a Positive Number", "code": "invalid_value"}]})
        if per_page > 100 or per_page < 1 or page < 1:
            return _err(400, {"description": "Validation failed", "errors": [{"field": "per_page", "message": "It should be a Positive Number less than or equal to 100", "code": "invalid_value"}]})
        chunk = items[(page - 1) * per_page: page * per_page]
        if page * per_page < len(items):
            params = dict(request.query_params) | {"page": str(page + 1)}
            headers = headers | {"Link": f'<{request.url.replace(query=urlencode(params))}>; rel="next"'}
        return JSONResponse(chunk, headers=headers)

    def contact_ref(cid: int) -> dict[str, Any] | None:
        c = next((c for c in d["contacts"] if c["id"] == cid), None)
        return c and {"id": c["id"], "name": c["name"], "email": c["email"], "phone": c["phone"], "mobile": c["mobile"]}

    def ticket_view(t: dict[str, Any], inc: list[str], for_list: bool) -> dict[str, Any]:
        out = {k: v for k, v in t.items() if not (for_list and k in ("description", "description_text", "attachments"))}
        if for_list and "description" in inc:
            out["description_text"], out["description"] = t["description_text"], t["description"]
        if "requester" in inc:
            out["requester"] = contact_ref(t["requester_id"])
        if "stats" in inc:
            out["stats"] = {"first_responded_at": t["created_at"], "agent_responded_at": t["updated_at"], "requester_responded_at": None, "resolved_at": t["updated_at"] if t["status"] >= 4 else None, "closed_at": t["updated_at"] if t["status"] == 5 else None}
        if "conversations" in inc and not for_list:
            out["conversations"] = d["conversations"].get(t["id"], [])[-10:]
        return out

    async def agents_me(request: Request):
        h = guard(request)
        return h if isinstance(h, JSONResponse) else JSONResponse(d["agents"][0], headers=h)

    async def agents(request: Request):
        h = guard(request)
        return h if isinstance(h, JSONResponse) else paginate(request, d["agents"], h, 50)

    async def groups(request: Request):
        h = guard(request)
        return h if isinstance(h, JSONResponse) else paginate(request, d["groups"], h, 50)

    async def tickets(request: Request):
        inc = includes(request)
        h = guard(request, 1 + len(inc))
        if isinstance(h, JSONResponse):
            return h
        qp = request.query_params
        items = list(d["tickets"])
        if since := qp.get("updated_since"):
            items = [t for t in items if t["updated_at"] >= since.replace(" ", "T")[:19]]
        else:  # Freshdesk default: only tickets created in the past 30 days
            cutoff = _iso(d["now"] - timedelta(days=30))
            items = [t for t in items if t["created_at"] >= cutoff]
        if rid := qp.get("requester_id"):
            items = [t for t in items if str(t["requester_id"]) == rid]
        flt = qp.get("filter")
        if flt == "new_and_my_open":
            items = [t for t in items if t["status"] == 2 and t["responder_id"] in (None, 101)]
        elif flt in ("watching", "spam", "deleted"):
            items = []  # the fictional dataset has no watched, spam or deleted tickets
        elif flt:
            return _err(400, {"description": "Validation failed", "errors": [{"field": "filter", "message": "It should be one of these values: 'new_and_my_open,watching,spam,deleted'", "code": "invalid_value"}]})
        key = qp.get("order_by", "created_at")
        items.sort(key=lambda t: t.get(key) or "", reverse=qp.get("order_type", "desc") == "desc")
        return paginate(request, [ticket_view(t, inc, True) for t in items], h)

    async def ticket(request: Request):
        inc = includes(request)
        h = guard(request, 1 + len(inc))
        if isinstance(h, JSONResponse):
            return h
        t = next((t for t in d["tickets"] if t["id"] == int(request.path_params["id"])), None)
        if not t:
            return _err(404, {})
        return JSONResponse(ticket_view(t, inc, False), headers=h)

    async def ticket_conversations(request: Request):
        h = guard(request)
        if isinstance(h, JSONResponse):
            return h
        tid = int(request.path_params["id"])
        if tid not in d["conversations"]:
            return _err(404, {})
        return paginate(request, d["conversations"][tid], h)

    async def search(request: Request):
        h = guard(request)
        if isinstance(h, JSONResponse):
            return h
        entity = request.path_params["entity"]
        raw = request.query_params.get("query", "")
        if not (raw.startswith('"') and raw.endswith('"')) or len(raw) < 3:
            return _err(400, {"description": "Validation failed", "errors": [{"field": "query", "message": "Query should be enclosed in double quotes", "code": "invalid_value"}]})
        q = raw[1:-1]
        if len(q) > 512:
            return _err(400, {"description": "Validation failed", "errors": [{"field": "query", "message": "Query length exceeds 512 characters", "code": "invalid_value"}]})
        try:
            page = int(request.query_params.get("page", 1))
            if not 1 <= page <= 10:
                raise ValueError
        except ValueError:
            return _err(400, {"description": "Validation failed", "errors": [{"field": "page", "message": "It should be a number between 1 and 10", "code": "invalid_value"}]})
        source = {"tickets": d["tickets"], "contacts": d["contacts"], "companies": d["companies"]}.get(entity)
        if source is None:
            return _err(404, {})
        try:
            node = _parse(_tokenize(q))
            if entity == "tickets":
                _validate_fields(node, _TICKET_FIELDS)
        except QueryError as e:
            return _err(400, {"description": "Validation failed", "errors": [{"field": "query", "message": str(e), "code": "invalid_value"}]})
        results = sorted((r for r in source if _matches(node, r)), key=lambda r: r.get("updated_at", ""), reverse=True)
        view = [ticket_view(r, [], True) for r in results] if entity == "tickets" else results
        return JSONResponse({"results": view[(page - 1) * 30: page * 30], "total": len(results)}, headers=h)

    async def contacts(request: Request):
        h = guard(request)
        if isinstance(h, JSONResponse):
            return h
        qp = request.query_params
        items = d["contacts"]
        for field in ("email", "phone", "mobile", "company_id"):
            if v := qp.get(field):
                items = [c for c in items if str(c.get(field) or "").lower() == v.lower()]
        return paginate(request, items, h)

    async def contacts_autocomplete(request: Request):
        h = guard(request)
        if isinstance(h, JSONResponse):
            return h
        term = request.query_params.get("term", "").lower()
        hits = [{"id": c["id"], "name": c["name"]} for c in d["contacts"] if any(p.startswith(term) for p in c["name"].lower().split()) or c["name"].lower().startswith(term)]
        return JSONResponse(hits[:20], headers=h)

    async def contact(request: Request):
        h = guard(request)
        if isinstance(h, JSONResponse):
            return h
        c = next((c for c in d["contacts"] if c["id"] == int(request.path_params["id"])), None)
        return JSONResponse(c, headers=h) if c else _err(404, {})

    async def companies(request: Request):
        h = guard(request)
        return h if isinstance(h, JSONResponse) else paginate(request, d["companies"], h)

    async def company(request: Request):
        h = guard(request)
        if isinstance(h, JSONResponse):
            return h
        c = next((c for c in d["companies"] if c["id"] == int(request.path_params["id"])), None)
        return JSONResponse(c, headers=h) if c else _err(404, {})

    async def ticket_fields(request: Request):
        h = guard(request)
        if isinstance(h, JSONResponse):
            return h
        fields = [
            {"name": "status", "label": "Status", "type": "default_status", "default": True, "choices": {"2": ["Open", "Being Processed"], "3": ["Pending", "Awaiting your Reply"], "4": ["Resolved", "This ticket has been Resolved"], "5": ["Closed", "This ticket has been Closed"], "6": ["Waiting on Gateway", "Being Processed"]}},
            {"name": "priority", "label": "Priority", "type": "default_priority", "default": True, "choices": {"Low": 1, "Medium": 2, "High": 3, "Urgent": 4}},
            {"name": "ticket_type", "label": "Type", "type": "default_ticket_type", "default": True, "choices": ["Question", "Incident", "Problem", "Feature Request", "Refund"]},
            {"name": "cf_order_id", "label": "Order ID", "type": "custom_text", "default": False},
            {"name": "cf_payment_method", "label": "Payment method", "type": "custom_dropdown", "default": False, "choices": ["UPI", "Card", "Netbanking", "Wallet"]},
        ]
        return JSONResponse(fields, headers=h)

    routes = [
        Route("/api/v2/agents/me", agents_me),
        Route("/api/v2/agents", agents),
        Route("/api/v2/groups", groups),
        Route("/api/v2/tickets", tickets),
        Route("/api/v2/tickets/{id:int}", ticket),
        Route("/api/v2/tickets/{id:int}/conversations", ticket_conversations),
        Route("/api/v2/search/{entity}", search),
        Route("/api/v2/contacts", contacts),
        Route("/api/v2/contacts/autocomplete", contacts_autocomplete),
        Route("/api/v2/contacts/{id:int}", contact),
        Route("/api/v2/companies", companies),
        Route("/api/v2/companies/{id:int}", company),
        Route("/api/v2/ticket_fields", ticket_fields),
    ]
    app = Starlette(routes=routes)
    app.state.mock = st
    return app

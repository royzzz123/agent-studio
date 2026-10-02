import pytest
from mcp.client import Client

from freshdesk_connector.server import build_server


@pytest.fixture
def server(make_client):
    client = make_client()

    async def provide():
        return client

    srv = build_server(provide, redact_pii=False)
    srv.test_client = client
    return srv


async def call(server, tool_name, **args):
    async with Client(server) as c:
        return await c.call_tool(tool_name, args)


async def test_all_tools_are_read_only(server):
    async with Client(server) as c:
        tools = (await c.list_tools()).tools
    assert len(tools) == 13
    assert all(t.annotations.read_only_hint and not t.annotations.destructive_hint for t in tools)
    assert all(t.description for t in tools)


async def test_search_tickets_end_to_end(server):
    r = await call(server, "search_tickets", status=["open", "pending"], priority=["urgent", "high"], tags=["refund"])
    assert not r.is_error
    out = r.structured_content
    assert out["query"] == "(status:2 OR status:3) AND (priority:4 OR priority:3) AND tag:'refund'"
    assert out["count"] > 0
    assert all(t["status"] in ("open", "pending") and "refund" in t["tags"] for t in out["tickets"])


async def test_get_ticket_shapes_labels_and_conversations(server):
    out = (await call(server, "get_ticket", ticket_id=1001)).structured_content["ticket"]
    assert out["status"] in ("open", "pending", "resolved", "closed")
    assert out["requester"]["email"].endswith("@example.com")
    assert out["conversations"][0]["author"] in ("customer", "agent")
    assert out["custom_fields"]["cf_order_id"].startswith("ORD-")


async def test_conversation_pagination(server):
    tid = 1001  # every 9th ticket has a long thread
    r = (await call(server, "list_ticket_conversations", ticket_id=tid, per_page=5)).structured_content
    assert r["count"] == 5 and r["has_more"] and r["next_page"] == 2


async def test_find_tickets_by_text_scans_subjects(server):
    out = (await call(server, "find_tickets_by_text", text="refund not received", updated_within_days=60)).structured_content
    assert out["count"] > 0
    assert all("refund not received" in t["subject"].lower() for t in out["tickets"])
    assert out["coverage"].startswith("all tickets")


async def test_find_contacts_and_profile(server):
    by_name = (await call(server, "find_contacts", name="aarav")).structured_content
    cid = by_name["contacts"][0]["id"]
    prof = (await call(server, "get_contact", contact_id=cid)).structured_content
    assert prof["contact"]["name"].startswith("Aarav")
    assert all(t["requester_id"] == cid for t in prof["recent_tickets"])
    email = prof["contact"]["email"]
    by_email = (await call(server, "find_contacts", email=email)).structured_content
    assert by_email["contacts"][0]["id"] == cid


async def test_errors_are_agent_readable(server):
    r = await call(server, "get_ticket", ticket_id=424242)
    assert r.is_error and "not found" in r.content[0].text.lower() and "search tool" in r.content[0].text
    r = await call(server, "search_tickets")
    assert r.is_error and "at least one filter" in r.content[0].text
    r = await call(server, "find_contacts", email="a@example.com", name="ab")
    assert r.is_error and "exactly one" in r.content[0].text


async def test_describe_fields_teaches_custom_status(server):
    fields = (await call(server, "describe_ticket_fields")).structured_content["fields"]
    status = next(f for f in fields if f["name"] == "status")
    assert status["choices"]["6"] == "Waiting on Gateway"


async def test_connector_status_reports_quota(server):
    out = (await call(server, "connector_status")).structured_content
    assert out["connected"] and out["quota"]["per_minute"] == 1000


async def test_pii_redaction(make_client):
    client = make_client()

    async def provide():
        return client

    srv = build_server(provide, redact_pii=True)
    out = (await call(srv, "get_ticket", ticket_id=1001)).structured_content["ticket"]
    assert "***@example.com" in out["requester"]["email"]
    assert out["requester"]["phone"].startswith("***")

#!/usr/bin/env python3
"""End-to-end test: launch `freshdesk-connector serve` as a real MCP stdio subprocess and exercise every tool.

  uv run scripts/smoke_test.py                 # against the bundled mock Freshdesk (no account needed)
  uv run scripts/smoke_test.py --target live   # against your helpdesk (uses `auth login` credentials); read-only
  uv run scripts/smoke_test.py --burst         # also run a real-time rate-limit demo (~70s)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from typing import Any

from mcp.client import Client
from mcp.client.stdio import StdioServerParameters

MOCK_KEY = "mock-api-key"
RESULTS: list[tuple[str, bool, str, float]] = []


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def mock_freshdesk(rate_limit: int):
    port = free_port()
    proc = subprocess.Popen([sys.executable, "-m", "freshdesk_connector.cli", "mock", "--port", str(port), "--rate-limit", str(rate_limit)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.1).close()
                break
            except OSError:
                time.sleep(0.1)
        yield f"http://127.0.0.1:{port}"
    finally:
        proc.terminate()
        proc.wait(5)


def server_params(env_overrides: dict[str, str]) -> StdioServerParameters:
    env = {k: v for k, v in os.environ.items() if not k.startswith("FRESHDESK_")} | env_overrides
    return StdioServerParameters(command=sys.executable, args=["-m", "freshdesk_connector.cli", "serve"], env=env)


async def call(c: Client, tool: str, **args: Any):
    r = await c.call_tool(tool, args)
    text = r.content[0].text if r.content else ""
    return r.is_error, (r.structured_content if not r.is_error else text)


async def check(name: str, coro, predicate) -> Any:
    t0 = time.perf_counter()
    try:
        is_error, out = await coro
        ok, detail = predicate(is_error, out)
    except Exception as e:  # noqa: BLE001
        ok, detail, out = False, f"{type(e).__name__}: {e}", None
    RESULTS.append((name, ok, detail, time.perf_counter() - t0))
    return out


async def run_suite(params: StdioServerParameters, live: bool) -> None:
    async with Client(params) as c:
        tools = (await c.list_tools()).tools
        RESULTS.append(("tools/list: 13 read-only tools", len(tools) == 13 and all(t.annotations.read_only_hint for t in tools), f"{len(tools)} tools", 0.0))

        await check("connector_status (auth + quota)", call(c, "connector_status"), lambda e, o: (not e and o["connected"], f"as {o.get('authenticated_as')}, quota {o['quota']['per_minute']}/min" if not e else o))

        p1 = await check("list_tickets page 1", call(c, "list_tickets", per_page=5), lambda e, o: (not e and o["count"] > 0, f"{o['count']} tickets, has_more={o['has_more']}" if not e else o))
        if not p1 or not p1.get("tickets"):
            return
        if p1["has_more"]:
            await check("list_tickets page 2 (no overlap)", call(c, "list_tickets", per_page=5, page=2), lambda e, o: (not e and not {t["id"] for t in o["tickets"]} & {t["id"] for t in p1["tickets"]}, f"{o['count']} new tickets" if not e else o))

        first = p1["tickets"][0]
        t = await check(f"get_ticket #{first['id']}", call(c, "get_ticket", ticket_id=first["id"]), lambda e, o: (not e and o["ticket"]["id"] == first["id"], f"status={o['ticket']['status']}, {len(o['ticket'].get('conversations', []))} convs" if not e else o))
        await check("list_ticket_conversations", call(c, "list_ticket_conversations", ticket_id=first["id"], per_page=5), lambda e, o: (not e, f"{o['count']} items" if not e else o))

        await check("search_tickets open+pending", call(c, "search_tickets", status=["open", "pending"]), lambda e, o: (not e and all(x["status"] in ("open", "pending") for x in o["tickets"]), f"total={o.get('total')} query={o['query']}" if not e else o))
        if not live:
            await check("search_tickets urgent refunds unassigned", call(c, "search_tickets", priority=["urgent", "high"], tags=["refund"], status=["open"]), lambda e, o: (not e, f"total={o.get('total')}" if not e else o))
        await check("search_tickets invalid status -> tool error", call(c, "search_tickets", status=["escalated"]), lambda e, o: (e and "Unknown status" in o, o[:90] if e else "expected error"))
        await check("search_tickets unsupported raw field -> tool error", call(c, "search_tickets", raw_query="subject:'refund'"), lambda e, o: (e, o[:90] if e else "expected error"))
        await check("get_ticket missing -> not found", call(c, "get_ticket", ticket_id=987654321), lambda e, o: (e and "not found" in o.lower(), o[:90] if e else "expected error"))

        word = next((w for w in first["subject"].split() if len(w) > 4 and w.isalpha()), first["subject"].split()[0])
        await check(f"find_tickets_by_text '{word}'", call(c, "find_tickets_by_text", text=word, updated_within_days=60), lambda e, o: (not e and o["count"] >= 1, f"{o['count']} matches / {o['tickets_scanned']} scanned" if not e else o))

        req = (t or {}).get("ticket", {}).get("requester") if t else None
        if req and req.get("email"):
            await check("find_contacts by email", call(c, "find_contacts", email=req["email"]), lambda e, o: (not e and o["count"] >= 1, f"id={o['contacts'][0]['id']}" if not e and o["contacts"] else o))
            await check("get_contact + recent tickets", call(c, "get_contact", contact_id=req["id"]), lambda e, o: (not e, f"{len(o.get('recent_tickets', []))} recent tickets" if not e else o))
        await check("find_contacts needs one criterion", call(c, "find_contacts"), lambda e, o: (e, o[:80] if e else "expected error"))
        await check("list_agents", call(c, "list_agents"), lambda e, o: (not e and o["count"] >= 1, f"{o['count']} agents" if not e else o))
        await check("list_groups", call(c, "list_groups"), lambda e, o: (not e, f"{o['count']} groups" if not e else o))
        await check("list_companies", call(c, "list_companies"), lambda e, o: (not e, f"{o['count']} companies" if not e else o))
        await check("describe_ticket_fields", call(c, "describe_ticket_fields"), lambda e, o: (not e and any(f["name"] == "status" for f in o["fields"]), f"{len(o['fields'])} fields" if not e else o))


async def run_bad_key(base_url: str) -> None:
    async with Client(server_params({"FRESHDESK_BASE_URL": base_url, "FRESHDESK_API_KEY": "not-a-real-key"})) as c:
        await check("invalid API key -> auth error (key not echoed)", call(c, "connector_status"), lambda e, o: (e and "invalid" in o and "not-a-real-key" not in o, o[:90] if e else "expected error"))


async def run_burst(base_url: str) -> None:
    """25 calls against a helpdesk whose quota is 20/min, with the client budget set far too high (600/min).
    The connector must finish with zero failures by reading quota headers and waiting for 429 Retry-After."""
    env = {"FRESHDESK_BASE_URL": base_url, "FRESHDESK_API_KEY": MOCK_KEY, "FRESHDESK_RATE_LIMIT_PER_MINUTE": "600"}
    async with Client(server_params(env)) as c:
        t0 = time.perf_counter()
        errors = 0
        for _ in range(25):
            e, _ = await call(c, "list_agents")
            errors += e
        _, status = await call(c, "connector_status")
        RESULTS.append(("burst: 25 calls vs 20/min quota, 0 failures", errors == 0, f"{errors} errors, {time.perf_counter() - t0:.0f}s, throttled={status['quota']['throttled_responses']}", time.perf_counter() - t0))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", choices=["mock", "live"], default="mock")
    ap.add_argument("--burst", action="store_true", help="real-time rate-limit demo against the mock (~70s)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.target == "live":
        asyncio.run(run_suite(server_params({k: v for k, v in os.environ.items() if k.startswith("FRESHDESK_")}), live=True))
    else:
        with mock_freshdesk(rate_limit=200) as url:
            asyncio.run(run_suite(server_params({"FRESHDESK_BASE_URL": url, "FRESHDESK_API_KEY": MOCK_KEY}), live=False))
            asyncio.run(run_bad_key(url))
        if args.burst:
            with mock_freshdesk(rate_limit=20) as url:
                asyncio.run(run_burst(url))

    if args.json:
        print(json.dumps([{"case": n, "pass": ok, "detail": d} for n, ok, d, _ in RESULTS], indent=2))
    else:
        width = max(len(n) for n, *_ in RESULTS)
        for name, ok, detail, dt in RESULTS:
            print(f"{'PASS' if ok else 'FAIL'}  {name:<{width}}  {dt * 1000:6.0f}ms  {str(detail)[:100]}")
        print(f"\n{sum(ok for _, ok, *_ in RESULTS)}/{len(RESULTS)} passed")
    return 0 if all(ok for _, ok, *_ in RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())

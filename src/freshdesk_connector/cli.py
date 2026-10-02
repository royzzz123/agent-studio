"""Command-line entry point: auth flow, MCP server, mock server, spec export."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import os
import sys
from pathlib import Path

from .client import FreshdeskClient
from .config import Credentials, base_url_from_domain, delete_credentials, load_credentials, save_credentials, validate_base_url
from .errors import FreshdeskError


async def _verify(creds: Credentials) -> dict:
    async with FreshdeskClient(creds) as client:
        me = await client.me()
        return {"name": (me.get("contact") or {}).get("name"), "email": (me.get("contact") or {}).get("email"), "quota_per_minute": client.quota.total}


def cmd_auth_login(args: argparse.Namespace) -> int:
    if args.base_url:
        base_url = validate_base_url(args.base_url)
    else:
        domain = args.domain or input("Freshdesk domain (e.g. acme or acme.freshdesk.com): ").strip()
        base_url = base_url_from_domain(domain)
    print("Find your API key in Freshdesk: profile picture (top right) > Profile settings > View API key.")
    api_key = os.environ.get("FRESHDESK_API_KEY") if args.key_from_env else getpass.getpass("API key (input hidden): ").strip()
    if not api_key:
        print("No API key provided.", file=sys.stderr)
        return 2
    creds = Credentials(base_url, api_key, "login")
    print(f"Verifying against {base_url} ...")
    try:
        who = asyncio.run(_verify(creds))
    except FreshdeskError as e:
        print(f"Verification failed: {e.agent_message()}", file=sys.stderr)
        return 1
    where = save_credentials(creds)
    print(f"Authenticated as {who['name']} <{who['email']}>. Credentials saved to {where}.")
    if who["quota_per_minute"]:
        print(f"Helpdesk API quota: {who['quota_per_minute']} credits/minute (shared by all integrations).")
    return 0


def cmd_auth_status(_: argparse.Namespace) -> int:
    try:
        creds = load_credentials()
        print(f"Source:   {creds.source}\nEndpoint: {creds.base_url}\nAPI key:  {creds.masked_key}")
        who = asyncio.run(_verify(creds))
    except FreshdeskError as e:
        print(e.agent_message(), file=sys.stderr)
        return 1
    print(f"Status:   OK, authenticated as {who['name']} <{who['email']}>, quota {who['quota_per_minute']}/min")
    return 0


def cmd_auth_logout(_: argparse.Namespace) -> int:
    removed = delete_credentials()
    print(f"Removed stored credentials from: {', '.join(removed)}" if removed else "No stored credentials found.")
    if os.environ.get("FRESHDESK_API_KEY"):
        print("Note: FRESHDESK_API_KEY is still set in your environment.")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from .server import build_server

    # stdout is the MCP channel on stdio; logs must go to stderr.
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    build_server().run(transport="stdio")
    return 0


def cmd_mock(args: argparse.Namespace) -> int:
    import uvicorn

    from .mock_server import MOCK_API_KEY, MockState, create_app

    print(f"Mock Freshdesk at http://{args.host}:{args.port}/api/v2  (API key: {MOCK_API_KEY}, limit {args.rate_limit}/min)")
    print(f"  export FRESHDESK_BASE_URL=http://{args.host}:{args.port} FRESHDESK_API_KEY={MOCK_API_KEY}")
    uvicorn.run(create_app(MockState(rate_limit_per_minute=args.rate_limit)), host=args.host, port=args.port, log_level="warning")
    return 0


def cmd_export_spec(args: argparse.Namespace) -> int:
    from .server import build_server

    async def dump() -> list[dict]:
        tools = await build_server().list_tools()
        return [t.model_dump(by_alias=True, exclude_none=True, mode="json") for t in tools]

    spec = {"server": {"name": "freshdesk", "transport": "stdio", "command": "freshdesk-connector serve"}, "tools": asyncio.run(dump())}
    text = json.dumps(spec, indent=2)
    if args.output:
        Path(args.output).write_text(text + "\n")
        print(f"Wrote {len(spec['tools'])} tool definitions to {args.output}")
    else:
        print(text)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="freshdesk-connector", description="Freshdesk connector for Agent Studio (MCP)")
    sub = parser.add_subparsers(dest="command", required=True)

    auth = sub.add_parser("auth", help="Manage Freshdesk credentials").add_subparsers(dest="auth_command", required=True)
    login = auth.add_parser("login", help="Verify and store an API key (OS keychain preferred)")
    login.add_argument("--domain", help="e.g. acme or acme.freshdesk.com")
    login.add_argument("--base-url", help="Explicit API base URL (custom domain or local mock)")
    login.add_argument("--key-from-env", action="store_true", help="Read the key from FRESHDESK_API_KEY instead of prompting")
    login.set_defaults(func=cmd_auth_login)
    auth.add_parser("status", help="Show which credentials are in use and verify them").set_defaults(func=cmd_auth_status)
    auth.add_parser("logout", help="Delete stored credentials").set_defaults(func=cmd_auth_logout)

    sub.add_parser("serve", help="Run the MCP server over stdio").set_defaults(func=cmd_serve)

    mock = sub.add_parser("mock", help="Run a local mock Freshdesk API with fictional data")
    mock.add_argument("--host", default="127.0.0.1")
    mock.add_argument("--port", type=int, default=8765)
    mock.add_argument("--rate-limit", type=int, default=50, help="Requests per minute before returning 429")
    mock.set_defaults(func=cmd_mock)

    spec = sub.add_parser("export-spec", help="Print the MCP tool specification as JSON")
    spec.add_argument("-o", "--output")
    spec.set_defaults(func=cmd_export_spec)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except FreshdeskError as e:
        print(e.agent_message(), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())

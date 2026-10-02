#!/usr/bin/env python3
"""Seed a Freshdesk *trial* helpdesk with fictional tickets for demoing the connector.

This is the only part of the project that writes to Freshdesk, and it is a dev tool, not an agent tool.
Every record it creates is tagged `seed-demo` and uses reserved example.com addresses.

  uv run scripts/seed_freshdesk.py --count 30        # create
  uv run scripts/seed_freshdesk.py --cleanup         # delete (moves to Trash) everything tagged seed-demo
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sys

from freshdesk_connector.client import FreshdeskClient
from freshdesk_connector.config import load_credentials
from freshdesk_connector.errors import FreshdeskError
from freshdesk_connector.mock_server import FIRST, LAST, SCENARIOS

TAG = "seed-demo"
FOLLOW_UPS = [
    (True, "Any update on this? It has been a few days and I still don't have a resolution."),
    (False, "Internal: checked the payment gateway dashboard. Payment captured; order sync failed. Escalating to finance."),
    (True, "I have attached my bank statement screenshot. Please expedite."),
]


async def seed(client: FreshdeskClient, count: int) -> None:
    rng = random.Random(42)
    for n in range(count):
        subject, body, tags, _type, _group = SCENARIOS[n % len(SCENARIOS)]
        first, last = rng.choice(FIRST), rng.choice(LAST)
        order = f"ORD-{30000 + n * 11}"
        payload = {
            "name": f"{first} {last}",
            "email": f"{first.lower()}.{last.lower()}.{n}@example.com",
            "subject": f"{subject} - {order}",
            "description": body.format(order=order),
            "status": rng.choices([2, 3, 4, 5], weights=[4, 2, 2, 1])[0],
            "priority": 4 if "duplicate-charge" in tags else rng.choice([1, 2, 2, 3, 3, 4]),
            "source": rng.choice([1, 2, 3, 7]),
            "tags": [*tags, TAG],
        }
        resp = await client.request("POST", "tickets", json=payload)
        tid = resp.json()["id"]
        for incoming, text in FOLLOW_UPS[: rng.randrange(0, len(FOLLOW_UPS) + 1)]:
            # Notes (not replies) so Freshdesk never emails the fictional requester.
            await client.request("POST", f"tickets/{tid}/notes", json={"body": text, "private": not incoming, "incoming": incoming})
        print(f"  created #{tid}: {payload['subject']}")


async def cleanup(client: FreshdeskClient) -> None:
    deleted = 0
    while True:
        page = await client.search("tickets", f"tag:'{TAG}'")
        if not page:
            break
        for t in page:
            await client.request("DELETE", f"tickets/{t['id']}")
            deleted += 1
        print(f"  deleted {deleted} so far (search index can lag; re-run if some remain)")
        if not page.has_more:
            break
    print(f"Deleted {deleted} tickets tagged {TAG}.")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=30)
    ap.add_argument("--cleanup", action="store_true")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = ap.parse_args()

    creds = load_credentials()
    action = f"DELETE all tickets tagged '{TAG}'" if args.cleanup else f"CREATE {args.count} fictional tickets"
    if not args.yes and input(f"This will {action} in {creds.base_url}. Continue? [y/N] ").lower() != "y":
        return 1
    async with FreshdeskClient(creds) as client:
        try:
            await (cleanup(client) if args.cleanup else seed(client, args.count))
        except FreshdeskError as e:
            print(e.agent_message(), file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

# Freshdesk connector for Agent Studio

A private connector that gives an Agent Studio agent **read-only** access to a merchant's Freshdesk helpdesk
(tickets, conversations, contacts, companies, agents, groups), packaged as an **MCP server**.

- **Auth:** API-key flow with live verification and OS-keychain storage (`auth login | status | logout`)
- **Primitives:** list / get / search for tickets, conversations, contacts and companies, plus keyword scan and schema discovery (13 tools)
- **Rate limits:** quota-aware token bucket, adaptive slow-down from `X-RateLimit-*` headers, `Retry-After` handling, jittered backoff
- **Spec:** MCP tool specification in [`tools.json`](tools.json) (generated from code)
- **Docs:** [what the agent can / cannot do](docs/CAPABILITIES.md) · [design, limitations and the long-term fix](docs/DESIGN.md)
- **Runs without a Freshdesk account:** a bundled mock Freshdesk with fictional data powers the tests and demo

## Quick start (no Freshdesk account needed, about 2 minutes)

Requires Python ≥ 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync                                  # install
uv run pytest                            # 48 unit/integration tests (about 1s)
uv run scripts/smoke_test.py             # end-to-end: launches the MCP server over stdio against the mock, runs 20 cases
uv run scripts/smoke_test.py --burst     # + real-time rate-limit demo: 25 calls against a 20/min quota, 0 failures (~70s)
```

Explore it by hand with the mock:

```bash
uv run freshdesk-connector mock --port 8765          # terminal 1
export FRESHDESK_BASE_URL=http://127.0.0.1:8765 FRESHDESK_API_KEY=mock-api-key
npx @modelcontextprotocol/inspector uv run freshdesk-connector serve   # terminal 2: interactive MCP Inspector UI
```

## Using a real Freshdesk helpdesk

1. Create a free trial at freshdesk.com. Then go to your profile picture → **Profile settings** → **View API key**.
2. Authenticate (the key is entered hidden, verified live, and stored in your OS keychain):
   ```bash
   uv run freshdesk-connector auth login --domain yourcompany      # or yourcompany.freshdesk.com
   uv run freshdesk-connector auth status
   ```
3. Optional: fill the trial with fictional, Razorpay-merchant-style tickets (payment failures, refunds, autopay):
   ```bash
   uv run scripts/seed_freshdesk.py --count 30      # all tagged seed-demo; --cleanup removes them
   ```
4. Run the same end-to-end suite against it (read-only):
   ```bash
   uv run scripts/smoke_test.py --target live
   ```

For servers and CI, skip the keychain and use environment variables instead:
`FRESHDESK_DOMAIN=yourcompany FRESHDESK_API_KEY=... uv run freshdesk-connector serve`.

## Connecting it to an agent

Any MCP client works the same way. Register the stdio command:

```json
{
  "mcpServers": {
    "freshdesk": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/this/repo", "run", "freshdesk-connector", "serve"],
      "env": { "FRESHDESK_REDACT_PII": "false" }
    }
  }
}
```

That snippet goes into Agent Studio's MCP/tool configuration (or Claude Desktop / Claude Code / Cursor to try it locally).
The server also sends `instructions` that teach the agent the efficient call patterns.

**Example agent prompts:** *"Which urgent or high-priority payment-failure tickets are still open and unassigned?"* ·
*"Summarise every ticket from aarav.sharma0@example.com and draft a reply to the latest one"* ·
*"Which open refund tickets are due before Friday, grouped by agent?"*

## Tools

| Tool | Purpose |
|---|---|
| `list_tickets` | Paginated ticket list (views, `updated_since`, ordering) |
| `get_ticket` | One ticket in full, including requester, SLA stats and last 10 messages |
| `list_ticket_conversations` | Full thread, paginated |
| `search_tickets` | Typed filters (status, priority, tags, type, agent, group, unassigned, dates, custom fields) or `raw_query` |
| `find_tickets_by_text` | Keyword scan of recent ticket subjects/descriptions, with reported coverage |
| `describe_ticket_fields` | Statuses (incl. custom), types, custom fields |
| `find_contacts` / `get_contact` | Customers by email, phone, name or company; profile and recent tickets |
| `list_companies` / `get_company` | B2B accounts |
| `list_agents` / `list_groups` | Resolve people and teams to IDs |
| `connector_status` | Connectivity and live quota |

Regenerate the spec after changing tools: `uv run freshdesk-connector export-spec -o tools.json`.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `FRESHDESK_DOMAIN` / `FRESHDESK_BASE_URL` | – | Helpdesk (`acme`) or explicit API URL (custom domain, mock). Env takes priority over the keychain |
| `FRESHDESK_API_KEY` | – | API key (prefer `auth login` locally, a secret manager in deployment) |
| `FRESHDESK_QUOTA_SHARE` | `0.5` | Fraction of the account's per-minute quota this connector may use |
| `FRESHDESK_RATE_LIMIT_PER_MINUTE` | – | Fixed budget instead (still capped at the account quota) |
| `FRESHDESK_REDACT_PII` | `false` | Mask customer emails and phones in tool output |
| `FRESHDESK_CREDENTIAL_STORE` | keychain | Set to `file` to force the 0600 file store |

## Project layout

```
src/freshdesk_connector/
  cli.py          auth login/status/logout · serve · mock · export-spec
  server.py       MCP tools (read-only) and agent instructions
  client.py       Freshdesk v2 client: auth, pagination, retries, error mapping
  ratelimit.py    token bucket, quota tracking, retry policy
  query.py        typed filters → Freshdesk query language
  shaping.py      compact, labelled output; truncation; PII masking
  config.py       credential resolution and storage
  mock_server.py  offline Freshdesk with fictional data
scripts/          smoke_test.py (end-to-end over MCP) · seed_freshdesk.py (trial data)
tests/            pytest suite (runs against the in-process mock)
docs/             CAPABILITIES.md · DESIGN.md
tools.json        generated MCP tool specification
```

## Assumptions and limitations

- Targets Freshdesk REST API v2 with API-key auth. Freshdesk offers no OAuth flow for direct API access (see DESIGN.md).
- Read-only: no replies, updates or deletes. The agent drafts and a human sends.
- Keyword search is a bounded client-side scan of recent tickets, because Freshdesk has no free-text search API.
- One helpdesk per process. The rate limiter is per process.
- All bundled data is fictional (example.com / .test domains). No real customer data, keys or passwords are included.

Full list and the long-term fix: [docs/DESIGN.md](docs/DESIGN.md).

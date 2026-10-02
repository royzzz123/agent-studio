# Design notes, limitations and the long-term fix

## Architecture

```
Agent Studio agent ──MCP (stdio)──► server.py ── tools: validate args, shape output, map errors
                                       │
                                       ▼
                                   client.py ── auth · pagination · retries · error mapping
                                       │          ▲
                              ratelimit.py ───────┘  token bucket + quota headers + Retry-After
                                       │
                                       ▼
                         https://<merchant>.freshdesk.com/api/v2   (or mock_server.py locally)
```

| Module | Responsibility |
|---|---|
| `config.py` | Credential resolution (env > OS keychain > 0600 file); domain normalisation; refuses HTTP to non-local hosts |
| `client.py` | Async httpx client; Basic auth with the API key; `Link`-header pagination; search paging; typed errors |
| `ratelimit.py` | Token bucket, adaptive quota throttle, retry/backoff policy |
| `query.py` | Typed filters → Freshdesk query language, validated locally (length, quoting, parentheses) |
| `shaping.py` | Compact, labelled records; truncation; optional PII masking |
| `server.py` | 13 MCP tools with agent-oriented descriptions and server `instructions` |
| `mock_server.py` | Offline Freshdesk stand-in with fictional data and faithful rate limiting / errors |

## Key decisions

**API key auth, not OAuth.** Freshdesk's REST API v2 authenticates with a per-agent API key over HTTP Basic.
It has no OAuth authorization-code flow for direct API access. The "auth flow" here is the CLI's
`auth login`: it asks for the domain and key (hidden input), **verifies them live** against `/agents/me`, then stores them
in the OS keychain. `auth status` re-verifies, and `auth logout` revokes local storage. Deployed connectors take
`FRESHDESK_API_KEY` from a secret manager via env. Recommendation for merchants: create a dedicated
"integration" agent with a read-only role, so the key's blast radius and audit trail are separate from any human.

**Read-only by design.** An agent acting on customer-facing systems should earn write access step by step.
Reads are safe to automate. Writes (replies, status changes) need human-in-the-loop confirmation, idempotency
keys and audit logging, which is beyond what a 48-hour connector should claim to do safely.

**Defend the merchant's shared quota.** Freshdesk's quota is per account and shared with every other integration
(the merchant's CRM sync, Zapier, and so on). An agent that burns it can break the merchant's other workflows, which is worse
than a slow answer. So the client (1) budgets a *share* of the reported quota, (2) slows down when
`X-RateLimit-Remaining` drops below 10%, (3) honours `Retry-After` on 429 up to 65s, then (4) fails fast
with an agent-readable "wait N seconds" message instead of hanging. Calls with `include=` are charged their
real credit cost. Search page/length limits are validated locally so invalid queries never spend credits.

**Shape for the agent, not the API.** Raw tickets have about 40 fields with numeric codes. Tools return labels,
drop nulls, truncate long bodies, and add `has_more`/`next_page`/`total`, so the model paginates correctly. Errors
say what to do next ("use a list or search tool to find valid IDs") rather than raising raw HTTP codes.

**Fill the keyword-search gap honestly.** Merchants ask "tickets about refunds", but Freshdesk can't search
text. `find_tickets_by_text` scans recent tickets client-side and **reports its coverage** ("all tickets updated in the
last 30 days" vs "only the 300 most recent"), so the agent doesn't over-claim.

**A mock that behaves like the real API.** Reviewers and CI can run everything without credentials, and the same
mock drives unit tests (in-process via ASGI) and the end-to-end MCP smoke test (real subprocesses).

## Limitations of this solution

1. **Keyword search is approximate.** It's limited to recent tickets and subject/description, not conversation bodies.
2. **Polling, not events.** The agent only sees data when it asks. Nothing reacts to new tickets.
3. **Single tenant per process.** Credentials are per process, which suits stdio but not a hosted multi-merchant service.
4. **No caching.** Repeated lookups such as agents and groups spend quota each time.
5. **Mock fidelity.** The mock implements the subset of Freshdesk behaviour the connector uses. Edge cases of the real
   query language (e.g. custom-field operators) are validated only against live Freshdesk.
6. **Freshdesk API v2 semantics assumed.** These include the 30-day default window on `/tickets`, the 300-result search cap, the 10-conversation
   embed cap, and the per-minute quota with `Retry-After`. If Freshdesk changes these, the constants in `client.py` must follow.
7. **The rate limiter is per process.** Two connector instances against one helpdesk each budget independently. Both still
   respect the server's 429/quota headers, but the proactive budget is not shared.

## Long-term fix (roadmap)

| Problem | Long-term approach |
|---|---|
| Keyword / semantic search | Incremental sync (`updated_since` cursor + Freshdesk webhooks via automation rules) into a per-merchant index (Postgres FTS or a vector store) covering conversations too; the tool then queries the index |
| Freshness / proactive agents | Webhook ingestion → event bus, so agents can act on "new urgent payment-failure ticket" without polling |
| Multi-tenant hosting | Remote MCP over Streamable HTTP with Agent Studio-issued tokens; per-merchant credentials in a vault (KMS-encrypted), resolved per request; shared Redis-backed token bucket per helpdesk |
| Quota efficiency | Short-TTL cache for agents/groups/ticket_fields; ETag/`updated_since` deltas |
| Write actions | `add_private_note`, `draft_reply`, `update_status` behind explicit per-call human approval (MCP elicitation), idempotency keys, audit log, and `destructiveHint` annotations |
| Governance | Per-merchant field allow-lists, PII redaction on by default, structured audit logs of every tool call |
| OAuth | If moving to the Freshworks platform (Freshdesk Omni / marketplace app with OAuth), swap the credential provider in `config.py` and the `auth=` line in `client.py` (auth is isolated there) |

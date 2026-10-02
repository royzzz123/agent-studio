# What the agent can and cannot do

This connector gives an Agent Studio agent **read-only** access to one merchant's Freshdesk helpdesk.
It authenticates as the Freshdesk agent who owns the API key, so it sees exactly what that agent sees.

## The agent CAN

| Capability | Tool(s) | Notes |
|---|---|---|
| List recent tickets, newest first, with pagination | `list_tickets` | Views: all, new & my open, watching, spam, deleted |
| Read a full ticket: description, custom fields, requester, SLA stats, last 10 messages | `get_ticket` | Status, priority and source come back as labels ("open", "urgent", "whatsapp") |
| Read a ticket's full conversation thread | `list_ticket_conversations` | Tells customer, agent and private (internal) notes apart |
| Filter tickets by status, priority, tag, type, assignee, group, unassigned, created/updated/due dates, custom fields | `search_tickets` | Typed filters compile to Freshdesk's query language; `raw_query` for advanced use |
| Find tickets by keyword ("refund", "UPI") | `find_tickets_by_text` | Scans recently updated tickets client-side (see limits below) |
| Find a customer by email, phone, name prefix or company; see their profile and recent tickets | `find_contacts`, `get_contact` | |
| Look up companies, agents and groups (turn "Priya" or "Payments team" into IDs) | `list_companies`, `get_company`, `list_agents`, `list_groups` | |
| Learn the helpdesk's schema: custom statuses, ticket types, custom fields | `describe_ticket_fields` | Also teaches the connector custom status labels |
| Check connectivity and remaining API quota | `connector_status` | |

**Example questions it can answer:** "Which urgent payment-failure tickets are still open and unassigned?",
"Summarise everything customer aarav.sharma0@example.com has raised this month", "Which open tickets
breach SLA by Friday?", "What did the customer say last on #1042?", "How many refund tickets did the Payments
team resolve this week?"

## The agent CANNOT

| Limitation | Why / workaround |
|---|---|
| **Create, reply to, update, assign, merge, close or delete tickets, contacts or anything else** | Deliberately read-only (all tools are annotated `readOnlyHint`). Write tools need a per-action approval design first (see `DESIGN.md` → Roadmap). The agent can *draft* a reply for a human to send. |
| Full-text search across all history | Freshdesk's API has no free-text ticket search. `find_tickets_by_text` covers subjects (optionally descriptions) of tickets updated in the last ≤90 days, up to 500 tickets per call. Conversation bodies are not searched. |
| Get more than 300 results for one search | Freshdesk caps search at 10 pages × 30. The tool warns and asks for narrower filters. |
| See tickets the API-key owner can't see | Permissions follow the Freshdesk agent role (e.g. group-restricted agents). Use an admin or a dedicated read-only "integration agent". |
| See changes made in the last few seconds or minutes in `search_tickets` | Freshdesk's search index updates asynchronously. `get_ticket` / `list_tickets` are always current. |
| Download attachments | Only the name, type and size are returned. Signed download URLs are left out on purpose so they can't leak. |
| Read the knowledge base, canned responses, satisfaction ratings, time entries, Freshchat/Freshcaller data | Out of scope for v1. Each is an additional endpoint and tool. |
| Read more than ~2,000 characters of a single message | Bodies are truncated (marked as such) to protect the agent's context window. |
| Serve several merchants from one process | One process = one helpdesk credential. Multi-tenant hosting is on the roadmap. |
| Run unlimited queries | Each call spends the merchant's shared Freshdesk quota (plan-dependent, e.g. 50/min on trial). The connector budgets 50% of it by default and returns a clear "wait N seconds" error when exhausted. |

## Data handling

- Credentials: OS keychain or env vars; never logged and never returned in tool output or errors (keys are masked in `auth status`).
- `FRESHDESK_REDACT_PII=true` masks customer emails and phone numbers in all tool output (`a***@example.com`, `***1234`).
- No caching or persistence of helpdesk data. The connector is a pass-through.

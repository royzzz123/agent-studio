"""Compile typed filters into Freshdesk's filter-query language.

Freshdesk's `/search/*` endpoints take a query like
    "(status:2 OR status:3) AND priority:4 AND tag:'refund' AND created_at:>'2026-09-01'"
with these constraints, which we enforce before sending to save API credits:
  * at most 512 characters inside the quotes;
  * string values wrapped in single quotes; there is no escape syntax, so
    values containing quotes are rejected;
  * dates as 'YYYY-MM-DD', compared with :> (on/after) and :< (on/before);
  * only indexed fields are searchable (no free text on subject/description).
"""

from __future__ import annotations

import re
from datetime import date, datetime

from .errors import InvalidRequestError
from .shaping import PRIORITY_CODES, STATUS_CODES

MAX_QUERY_LEN = 512
_FIELD_RE = re.compile(r"^(cf_)?[a-z][a-z0-9_]{0,63}$")
_SAFE_STR_RE = re.compile(r"^[^'\"\\]{1,128}$")


def _date(value: str, name: str) -> str:
    try:
        return (datetime.fromisoformat(value).date() if "T" in value else date.fromisoformat(value)).isoformat()
    except ValueError as e:
        raise InvalidRequestError(f"{name} must be an ISO date like 2026-09-30, got '{value}'") from e


def _str(value: str, name: str) -> str:
    if not _SAFE_STR_RE.match(value):
        raise InvalidRequestError(f"{name} value '{value}' must be 1-128 chars with no quotes or backslashes")
    return f"'{value}'"


def _any_of(field: str, values: list[str]) -> str:
    clauses = [f"{field}:{v}" for v in values]
    return clauses[0] if len(clauses) == 1 else "(" + " OR ".join(clauses) + ")"


def _codes(values: list[str], mapping: dict[str, int], name: str) -> list[str]:
    out = []
    for v in values:
        key = str(v).strip().lower()
        if key.isdigit():
            out.append(key)
        elif key in mapping:
            out.append(str(mapping[key]))
        else:
            raise InvalidRequestError(f"Unknown {name} '{v}'. Valid: {', '.join(mapping)} (or a numeric code for custom values)")
    return out


def build_ticket_query(
    *,
    status: list[str] | None = None,
    priority: list[str] | None = None,
    tags: list[str] | None = None,
    ticket_type: str | None = None,
    agent_id: int | None = None,
    group_id: int | None = None,
    unassigned: bool = False,
    created_after: str | None = None,
    created_before: str | None = None,
    updated_after: str | None = None,
    updated_before: str | None = None,
    due_before: str | None = None,
    custom_fields: dict[str, str | int | bool] | None = None,
) -> str:
    clauses: list[str] = []
    if status:
        clauses.append(_any_of("status", _codes(status, STATUS_CODES, "status")))
    if priority:
        clauses.append(_any_of("priority", _codes(priority, PRIORITY_CODES, "priority")))
    if tags:
        clauses.append(_any_of("tag", [_str(t, "tag") for t in tags]))
    if ticket_type:
        clauses.append(f"type:{_str(ticket_type, 'ticket_type')}")
    if unassigned and agent_id is not None:
        raise InvalidRequestError("Use either agent_id or unassigned=true, not both")
    if unassigned:
        clauses.append("agent_id:null")
    elif agent_id is not None:
        clauses.append(f"agent_id:{int(agent_id)}")
    if group_id is not None:
        clauses.append(f"group_id:{int(group_id)}")
    for field, op, value in (
        ("created_at", ">", created_after),
        ("created_at", "<", created_before),
        ("updated_at", ">", updated_after),
        ("updated_at", "<", updated_before),
        ("due_by", "<", due_before),
    ):
        if value:
            clauses.append(f"{field}:{op}'{_date(value, field)}'")
    for name, value in (custom_fields or {}).items():
        field = name if name.startswith("cf_") else f"cf_{name}"
        if not _FIELD_RE.match(field):
            raise InvalidRequestError(f"Invalid custom field name '{name}'")
        if isinstance(value, bool):
            clauses.append(f"{field}:{str(value).lower()}")
        elif isinstance(value, int):
            clauses.append(f"{field}:{value}")
        else:
            clauses.append(f"{field}:{_str(str(value), field)}")
    if not clauses:
        raise InvalidRequestError("Provide at least one filter (e.g. status, priority, tags, or a date range)")
    return finalize(" AND ".join(clauses))


def finalize(query: str) -> str:
    """Validate a raw or built query body (without the outer double quotes)."""
    q = query.strip()
    if q.startswith('"') and q.endswith('"') and len(q) >= 2:
        q = q[1:-1].strip()
    if not q:
        raise InvalidRequestError("Query is empty")
    if len(q) > MAX_QUERY_LEN:
        raise InvalidRequestError(f"Query is {len(q)} chars; Freshdesk allows at most {MAX_QUERY_LEN}. Narrow the filters")
    if '"' in q or "\\" in q:
        raise InvalidRequestError("Query must not contain double quotes or backslashes; wrap string values in single quotes")
    if q.count("'") % 2:
        raise InvalidRequestError("Unbalanced single quotes in query")
    depth = 0
    for ch in re.sub(r"'[^']*'", "", q):
        depth += ch == "("
        depth -= ch == ")"
        if depth < 0:
            break
    if depth != 0:
        raise InvalidRequestError("Unbalanced parentheses in query")
    return q

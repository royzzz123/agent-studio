"""Turn raw Freshdesk payloads into compact, agent-friendly records.

Raw Freshdesk tickets carry about 40 fields, most of them null or numeric codes.
Agents reason better, and spend fewer tokens, on a smaller record with
human-readable labels, so every tool returns data shaped here.
"""

from __future__ import annotations

import re
from typing import Any

STATUS = {2: "open", 3: "pending", 4: "resolved", 5: "closed"}
PRIORITY = {1: "low", 2: "medium", 3: "high", 4: "urgent"}
SOURCE = {1: "email", 2: "portal", 3: "phone", 5: "twitter", 6: "facebook", 7: "chat", 8: "mobihelp", 9: "feedback_widget", 10: "outbound_email", 11: "ecommerce", 12: "bot", 13: "whatsapp"}

STATUS_CODES = {v: k for k, v in STATUS.items()}
PRIORITY_CODES = {v: k for k, v in PRIORITY.items()}

MAX_TEXT = 2000
_EMAIL_RE = re.compile(r"([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*(@[A-Za-z0-9.-]+\.[A-Za-z]{2,})")
_PHONE_RE = re.compile(r"(?<!\w)(\+?\d[\d\s-]{6,}\d)(?!\w)")


def truncate(text: str | None, limit: int = MAX_TEXT) -> str | None:
    if text is None:
        return None
    text = text.strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit]}… [truncated {len(text) - limit} chars; fetch conversations for full text]"


class Shaper:
    """Shapes records; optionally masks emails and phone numbers (FRESHDESK_REDACT_PII=true)."""

    def __init__(self, redact_pii: bool = False, status_labels: dict[int, str] | None = None):
        self.redact_pii = redact_pii
        self.status_labels = {**STATUS, **(status_labels or {})}

    def _pii(self, value: str | None) -> str | None:
        if not value or not self.redact_pii:
            return value
        value = _EMAIL_RE.sub(r"\1***\2", value)
        return _PHONE_RE.sub(lambda m: "***" + re.sub(r"\D", "", m.group(1))[-4:], value)

    def status(self, code: int | None) -> str | None:
        return None if code is None else self.status_labels.get(code, f"custom_status_{code}")

    def ticket(self, t: dict[str, Any], detail: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": t.get("id"),
            "subject": t.get("subject"),
            "status": self.status(t.get("status")),
            "priority": PRIORITY.get(t.get("priority"), t.get("priority")),
            "source": SOURCE.get(t.get("source"), t.get("source")),
            "type": t.get("type"),
            "tags": t.get("tags") or [],
            "requester_id": t.get("requester_id"),
            "responder_id": t.get("responder_id"),
            "group_id": t.get("group_id"),
            "company_id": t.get("company_id"),
            "created_at": t.get("created_at"),
            "updated_at": t.get("updated_at"),
            "due_by": t.get("due_by"),
            "first_response_due_by": t.get("fr_due_by"),
            "is_escalated": bool(t.get("is_escalated") or t.get("fr_escalated")),
        }
        if req := t.get("requester"):
            out["requester"] = self.contact_ref(req)
        if stats := t.get("stats"):
            out["stats"] = {k: stats.get(k) for k in ("first_responded_at", "agent_responded_at", "requester_responded_at", "resolved_at", "closed_at") if k in stats}
        if detail or t.get("description_text") is not None:
            out["description"] = self._pii(truncate(t.get("description_text")))
        if detail:
            cf = {k: v for k, v in (t.get("custom_fields") or {}).items() if v is not None}
            if cf:
                out["custom_fields"] = cf
            if t.get("attachments"):
                out["attachments"] = [self.attachment(a) for a in t["attachments"]]
            if "conversations" in t:
                convs = t.get("conversations") or []
                out["conversations"] = [self.conversation(c) for c in convs]
                out["conversations_note"] = "Embedded conversations are capped at 10 by Freshdesk; use list_ticket_conversations for the full thread."
        return {k: v for k, v in out.items() if v is not None}

    def conversation(self, c: dict[str, Any]) -> dict[str, Any]:
        out = {
            "id": c.get("id"),
            "author": "customer" if c.get("incoming") else "agent",
            "private_note": bool(c.get("private")),
            "user_id": c.get("user_id"),
            "created_at": c.get("created_at"),
            "body": self._pii(truncate(c.get("body_text"))),
        }
        if c.get("attachments"):
            out["attachments"] = [self.attachment(a) for a in c["attachments"]]
        return out

    def attachment(self, a: dict[str, Any]) -> dict[str, Any]:
        # Download URLs are short-lived signed links; omit them so agents don't leak or cache them.
        return {"name": a.get("name"), "content_type": a.get("content_type"), "size_bytes": a.get("size")}

    def contact_ref(self, c: dict[str, Any]) -> dict[str, Any]:
        return {"id": c.get("id"), "name": c.get("name"), "email": self._pii(c.get("email")), "phone": self._pii(c.get("phone") or c.get("mobile"))}

    def contact(self, c: dict[str, Any]) -> dict[str, Any]:
        out = {
            "id": c.get("id"),
            "name": c.get("name"),
            "email": self._pii(c.get("email")),
            "phone": self._pii(c.get("phone")),
            "mobile": self._pii(c.get("mobile")),
            "company_id": c.get("company_id"),
            "job_title": c.get("job_title"),
            "active": c.get("active"),
            "tags": c.get("tags") or [],
            "created_at": c.get("created_at"),
            "updated_at": c.get("updated_at"),
        }
        return {k: v for k, v in out.items() if v is not None}

    def company(self, c: dict[str, Any]) -> dict[str, Any]:
        out = {
            "id": c.get("id"),
            "name": c.get("name"),
            "domains": c.get("domains") or [],
            "description": truncate(c.get("description"), 500),
            "industry": c.get("industry"),
            "account_tier": c.get("account_tier"),
            "created_at": c.get("created_at"),
        }
        return {k: v for k, v in out.items() if v is not None}

    def agent(self, a: dict[str, Any]) -> dict[str, Any]:
        contact = a.get("contact") or {}
        return {"id": a.get("id"), "name": contact.get("name"), "email": self._pii(contact.get("email")), "available": a.get("available"), "group_ids": a.get("group_ids", [])}

    def group(self, g: dict[str, Any]) -> dict[str, Any]:
        return {"id": g.get("id"), "name": g.get("name"), "description": g.get("description"), "agent_ids": g.get("agent_ids", [])}

"""Typed errors for the Freshdesk connector.

Every error carries an `agent_message()` that is safe to show to an LLM agent:
it explains what went wrong and what the agent (or user) can do next, and never
includes credentials.
"""

from __future__ import annotations

from typing import Any


class FreshdeskError(Exception):
    """Base class for all connector errors."""

    hint = "Try again later or check the connector configuration."

    def __init__(self, message: str, *, status: int | None = None, details: Any = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.details = details

    def agent_message(self) -> str:
        prefix = f"Freshdesk error {self.status}: " if self.status else "Freshdesk error: "
        return f"{prefix}{self.message}. {self.hint}"


class ConfigError(FreshdeskError):
    hint = "Run `freshdesk-connector auth login` or set FRESHDESK_DOMAIN and FRESHDESK_API_KEY."


class AuthError(FreshdeskError):
    hint = "The API key or helpdesk domain is invalid or was revoked. Ask the user to re-run `freshdesk-connector auth login`."


class PermissionDeniedError(FreshdeskError):
    hint = "The API key's agent role or the Freshdesk plan does not allow this. Do not retry; tell the user."


class NotFoundError(FreshdeskError):
    hint = "Check the ID. Use a list or search tool to find valid IDs instead of guessing."


class InvalidRequestError(FreshdeskError):
    hint = "Fix the arguments and try again."

    def agent_message(self) -> str:
        base = super().agent_message()
        if isinstance(self.details, list) and self.details:
            parts = [f"{e.get('field', '?')}: {e.get('message', e.get('code', 'invalid'))}" for e in self.details]
            base += " Field errors: " + "; ".join(parts)
        return base


class RateLimitedError(FreshdeskError):
    def __init__(self, message: str, *, retry_after: float | None = None, **kw: Any):
        super().__init__(message, status=429, **kw)
        self.retry_after = retry_after

    @property
    def hint(self) -> str:  # type: ignore[override]
        wait = f"about {int(self.retry_after)}s" if self.retry_after else "a minute"
        return f"The helpdesk's API quota is exhausted. Wait {wait} before calling Freshdesk again, and prefer fewer, more specific queries."


class UpstreamError(FreshdeskError):
    hint = "Freshdesk is having trouble or is unreachable. Retry later; this is not caused by the request."

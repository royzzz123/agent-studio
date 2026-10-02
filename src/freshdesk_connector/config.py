"""Credential resolution and storage.

Resolution order (first match wins):
  1. Environment: FRESHDESK_API_KEY plus FRESHDESK_DOMAIN or FRESHDESK_BASE_URL
     (meant for deployed connectors, where a secret manager injects env vars).
  2. OS keychain via `keyring` (macOS Keychain, Windows Credential Locker,
     Secret Service on Linux), written by `freshdesk-connector auth login`.
  3. A JSON file at ~/.config/freshdesk-connector/credentials.json with mode 0600,
     used only when no keychain is available.
"""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse

from .errors import ConfigError

KEYRING_SERVICE = "freshdesk-connector"
KEYRING_USER = "default"
CONFIG_DIR = Path(os.environ.get("FRESHDESK_CONNECTOR_HOME", Path.home() / ".config" / "freshdesk-connector"))
CREDENTIALS_FILE = CONFIG_DIR / "credentials.json"

_SUBDOMAIN_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


@dataclass(frozen=True)
class Credentials:
    base_url: str
    api_key: str
    source: str = "unknown"

    @property
    def masked_key(self) -> str:
        return mask_secret(self.api_key)

    def to_json(self) -> str:
        data = asdict(self)
        data.pop("source")
        return json.dumps(data)


def mask_secret(value: str) -> str:
    if len(value) <= 6:
        return "*" * len(value)
    return f"{value[:2]}{'*' * (len(value) - 6)}{value[-4:]}"


def base_url_from_domain(domain: str) -> str:
    """Accept 'acme', 'acme.freshdesk.com' or 'https://acme.freshdesk.com/' and return the v2 API base URL."""
    d = domain.strip().lower()
    if "://" in d:
        d = urlparse(d).hostname or ""
    d = d.rstrip("/").removesuffix(".freshdesk.com")
    if not _SUBDOMAIN_RE.match(d):
        raise ConfigError(f"'{domain}' is not a valid Freshdesk domain (expected e.g. 'acme' or 'acme.freshdesk.com')")
    return f"https://{d}.freshdesk.com/api/v2"


def validate_base_url(url: str) -> str:
    """Allow an explicit API base URL (custom domain, or the local mock). Plain HTTP only for localhost."""
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("https", "http") or not parsed.hostname:
        raise ConfigError(f"'{url}' is not a valid URL")
    if parsed.scheme == "http" and parsed.hostname not in _LOCAL_HOSTS:
        raise ConfigError("Refusing to send an API key over plain HTTP to a non-local host; use https")
    path = parsed.path.rstrip("/")
    if not path.endswith("/api/v2"):
        path += "/api/v2"
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def _from_env() -> Credentials | None:
    key = os.environ.get("FRESHDESK_API_KEY")
    if not key:
        return None
    if url := os.environ.get("FRESHDESK_BASE_URL"):
        return Credentials(validate_base_url(url), key, "env")
    if domain := os.environ.get("FRESHDESK_DOMAIN"):
        return Credentials(base_url_from_domain(domain), key, "env")
    raise ConfigError("FRESHDESK_API_KEY is set but neither FRESHDESK_DOMAIN nor FRESHDESK_BASE_URL is")


def _parse_stored(raw: str, source: str) -> Credentials:
    try:
        data = json.loads(raw)
        return Credentials(validate_base_url(data["base_url"]), data["api_key"], source)
    except (ValueError, KeyError, TypeError) as e:
        raise ConfigError(f"Stored credentials in {source} are corrupt; run `freshdesk-connector auth login` again") from e


def _keyring():
    if os.environ.get("FRESHDESK_CREDENTIAL_STORE") == "file":
        return None
    try:
        import keyring
        from keyring.backends.fail import Keyring as FailKeyring

        if isinstance(keyring.get_keyring(), FailKeyring):
            return None
        return keyring
    except Exception:
        return None


def _from_keyring() -> Credentials | None:
    kr = _keyring()
    if kr is None:
        return None
    try:
        raw = kr.get_password(KEYRING_SERVICE, KEYRING_USER)
    except Exception:
        return None
    return _parse_stored(raw, "keychain") if raw else None


def _from_file() -> Credentials | None:
    if not CREDENTIALS_FILE.exists():
        return None
    mode = stat.S_IMODE(CREDENTIALS_FILE.stat().st_mode)
    if mode & 0o077:
        raise ConfigError(f"{CREDENTIALS_FILE} is readable by other users (mode {oct(mode)}); run `chmod 600` on it")
    return _parse_stored(CREDENTIALS_FILE.read_text(), "file")


def load_credentials() -> Credentials:
    for loader in (_from_env, _from_keyring, _from_file):
        if creds := loader():
            return creds
    raise ConfigError("No Freshdesk credentials found")


def save_credentials(creds: Credentials) -> str:
    """Persist credentials, preferring the OS keychain. Returns where they were stored."""
    kr = _keyring()
    if kr is not None:
        try:
            kr.set_password(KEYRING_SERVICE, KEYRING_USER, creds.to_json())
            return "keychain"
        except Exception:
            pass
    CONFIG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(CREDENTIALS_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(creds.to_json())
    os.chmod(CREDENTIALS_FILE, 0o600)
    return f"file ({CREDENTIALS_FILE})"


def delete_credentials() -> list[str]:
    removed = []
    kr = _keyring()
    if kr is not None:
        try:
            kr.delete_password(KEYRING_SERVICE, KEYRING_USER)
            removed.append("keychain")
        except Exception:
            pass
    if CREDENTIALS_FILE.exists():
        CREDENTIALS_FILE.unlink()
        removed.append("file")
    return removed

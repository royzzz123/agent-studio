import os
import stat

import pytest

from freshdesk_connector import config
from freshdesk_connector.errors import ConfigError


@pytest.mark.parametrize("domain", ["acme", "ACME.freshdesk.com", "https://acme.freshdesk.com/", "acme.freshdesk.com/"])
def test_domain_normalisation(domain):
    assert config.base_url_from_domain(domain) == "https://acme.freshdesk.com/api/v2"


@pytest.mark.parametrize("domain", ["", "acme corp", "../etc", "-acme"])
def test_domain_rejects_garbage(domain):
    with pytest.raises(ConfigError):
        config.base_url_from_domain(domain)


def test_refuses_plain_http_to_remote_host():
    with pytest.raises(ConfigError):
        config.validate_base_url("http://evil.example.com")
    assert config.validate_base_url("http://127.0.0.1:8765") == "http://127.0.0.1:8765/api/v2"


def test_env_takes_precedence(monkeypatch):
    monkeypatch.setenv("FRESHDESK_API_KEY", "k" * 20)
    monkeypatch.setenv("FRESHDESK_DOMAIN", "acme")
    creds = config.load_credentials()
    assert creds.source == "env" and creds.base_url == "https://acme.freshdesk.com/api/v2"
    assert "k" * 20 not in creds.masked_key


def test_env_key_without_domain_is_an_error(monkeypatch):
    monkeypatch.setenv("FRESHDESK_API_KEY", "abc")
    monkeypatch.delenv("FRESHDESK_DOMAIN", raising=False)
    monkeypatch.delenv("FRESHDESK_BASE_URL", raising=False)
    with pytest.raises(ConfigError):
        config.load_credentials()


def test_file_store_roundtrip_with_0600(monkeypatch, tmp_path):
    for var in ("FRESHDESK_API_KEY", "FRESHDESK_DOMAIN", "FRESHDESK_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("FRESHDESK_CREDENTIAL_STORE", "file")
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "CREDENTIALS_FILE", tmp_path / "credentials.json")
    where = config.save_credentials(config.Credentials("https://acme.freshdesk.com/api/v2", "secret-key-123"))
    assert where.startswith("file")
    assert stat.S_IMODE(os.stat(tmp_path / "credentials.json").st_mode) == 0o600
    assert config.load_credentials().api_key == "secret-key-123"

    os.chmod(tmp_path / "credentials.json", 0o644)
    with pytest.raises(ConfigError, match="readable by other users"):
        config.load_credentials()
    assert config.delete_credentials() == ["file"]

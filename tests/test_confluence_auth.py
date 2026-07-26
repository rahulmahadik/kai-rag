"""Confluence authentication, every combination, single- and multi-instance.

Auth rules (per instance, independent of any other instance):
  - email + token  -> HTTP Basic auth (private space)
  - neither        -> anonymous (public space)
  - exactly one    -> configuration error, fails loudly

Construction is config-only (no network), so these assert the resolved auth object
directly. The key case is mixing a PRIVATE and a PUBLIC instance: a numbered
instance must NEVER inherit the flat instance's credentials."""

from __future__ import annotations

import httpx
import pytest

from kai.config import Settings
from kai.factory import _confluence_instances
from kai.providers.confluence_cloud import ConfluenceCloudKBSource


def _settings(**over):
    return Settings(_env_file=None, **over)


def _src(**over):
    return ConfluenceCloudKBSource(
        _settings(confluence_base_url="https://x/wiki", confluence_space_key="S", **over)
    )


# ---- single instance, all auth combinations -----------------------------------
def test_public_space_is_anonymous():
    assert _src()._auth is None  # no email, no token


def test_private_space_uses_basic_auth():
    src = _src(confluence_email="me@x.com", confluence_api_token="tok")
    assert isinstance(src._auth, httpx.BasicAuth)


def test_email_only_fails_loudly():
    with pytest.raises(ValueError, match="half-configured"):
        _src(confluence_email="me@x.com")


def test_token_only_uses_bearer_pat():
    # token without email = Server/Data Center Personal Access Token -> Bearer auth
    from kai.providers.confluence_cloud import _BearerAuth

    assert isinstance(_src(confluence_api_token="pat")._auth, _BearerAuth)


# ---- multi-instance: PRIVATE #1 + PUBLIC #2 (the inheritance trap) -------------
def test_private_then_public_each_keeps_own_auth(monkeypatch):
    monkeypatch.setenv("CONFLUENCE_2_BASE_URL", "https://public/wiki")
    monkeypatch.setenv("CONFLUENCE_2_SPACE_KEY", "PUB")  # no creds -> anonymous
    s = _settings(
        confluence_base_url="https://private/wiki",
        confluence_space_key="ENG",
        confluence_email="me@corp.com",
        confluence_api_token="secret",
    )
    auth = {
        i.confluence_base_url: ConfluenceCloudKBSource(i)._auth for i in _confluence_instances(s)
    }
    assert isinstance(auth["https://private/wiki"], httpx.BasicAuth)  # #1 private
    assert auth["https://public/wiki"] is None  # #2 NOT inheriting creds


# ---- multi-instance: PUBLIC #1 + PRIVATE #2 -----------------------------------
def test_public_then_private_each_keeps_own_auth(monkeypatch):
    monkeypatch.setenv("CONFLUENCE_2_BASE_URL", "https://private/wiki")
    monkeypatch.setenv("CONFLUENCE_2_SPACE_KEY", "DOCS")
    monkeypatch.setenv("CONFLUENCE_2_EMAIL", "me@corp.com")
    monkeypatch.setenv("CONFLUENCE_2_API_TOKEN", "secret")
    s = _settings(confluence_base_url="https://public/wiki", confluence_space_key="PUB")
    auth = {
        i.confluence_base_url: ConfluenceCloudKBSource(i)._auth for i in _confluence_instances(s)
    }
    assert auth["https://public/wiki"] is None  # #1 anonymous
    assert isinstance(auth["https://private/wiki"], httpx.BasicAuth)  # #2 private


# ---- multi-instance: a numbered instance half-configured still fails -----------
def test_numbered_half_configured_fails_loudly(monkeypatch):
    monkeypatch.setenv("CONFLUENCE_2_BASE_URL", "https://b/wiki")
    monkeypatch.setenv("CONFLUENCE_2_SPACE_KEY", "DOCS")
    monkeypatch.setenv("CONFLUENCE_2_EMAIL", "me@b.com")  # email but NO token -> error
    s = _settings(confluence_base_url="https://a/wiki", confluence_space_key="ENG")
    inst2 = next(i for i in _confluence_instances(s) if i.confluence_base_url == "https://b/wiki")
    with pytest.raises(ValueError, match="half-configured"):
        ConfluenceCloudKBSource(inst2)

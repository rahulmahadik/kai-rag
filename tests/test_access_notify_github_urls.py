"""Unit coverage for 1.2.0 seams: allowlists, notify, GitHub tracker, URL source."""

from __future__ import annotations

import httpx
import pytest

from kai.chat.access import deny_message, is_allowed
from kai.chat.notify import notify_user
from kai.config import Settings
from kai.providers.github_issues import GitHubIssuesTracker
from kai.providers.url_source import UrlKBSource, _html_to_text, _parse_sitemap


def _settings(**over) -> Settings:
    base = {"_env_file": None}
    base.update(over)
    return Settings(**base)


# --- access ---


def test_blank_allowlist_allows_anyone() -> None:
    assert is_allowed("anyone@x.co")
    assert is_allowed("U123")


def test_user_allowlist_is_case_insensitive() -> None:
    assert is_allowed("Alice@Ex.Com", approved_users="alice@ex.com, bob")
    assert not is_allowed("eve@ex.com", approved_users="alice@ex.com")


def test_domain_allowlist_matches_email_domain() -> None:
    assert is_allowed("a@acme.com", approved_domains="acme.com")
    assert not is_allowed("a@other.com", approved_domains="acme.com")
    assert not is_allowed("U123", approved_domains="acme.com")


def test_empty_identity_denied_when_allowlist_set() -> None:
    assert not is_allowed("", approved_users="a@b.co")
    assert deny_message()


# --- notify ---


def test_notify_webex_needs_token() -> None:
    ok, detail = notify_user(_settings(chat_platform="webex"), "a@b.co", "hi")
    assert not ok and "WEBEX_BOT_TOKEN" in detail


def test_notify_slack_needs_token() -> None:
    ok, detail = notify_user(_settings(chat_platform="slack"), "U123", "hi")
    assert not ok and "SLACK_BOT_TOKEN" in detail


def test_notify_teams_is_unsupported() -> None:
    ok, detail = notify_user(_settings(chat_platform="teams"), "x", "hi")
    assert not ok and "Teams" in detail


def test_notify_webex_delegates_to_send_dm(monkeypatch) -> None:
    monkeypatch.setattr("kai.chat.webex.send_direct_message", lambda *a, **k: True)
    ok, detail = notify_user(
        _settings(chat_platform="webex", webex_bot_token="tok"), "a@b.co", "hi"
    )
    assert ok and detail == "sent"


# --- GitHub Issues tracker ---


def test_github_tracker_requires_token_and_repo() -> None:
    with pytest.raises(ValueError, match="GITHUB_TOKEN"):
        GitHubIssuesTracker(_settings())
    with pytest.raises(ValueError, match="owner/name"):
        GitHubIssuesTracker(_settings(github_token="t", github_repo="norelslash"))


def test_github_tracker_creates_an_issue(monkeypatch) -> None:
    class _Resp:
        status_code = 201

        def json(self):
            return {"html_url": "https://github.com/acme/runbooks/issues/9"}

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, headers=None, json=None):
            assert "/repos/acme/runbooks/issues" in url
            assert json["title"]
            return _Resp()

    monkeypatch.setattr(httpx, "Client", _Client)
    tracker = GitHubIssuesTracker(_settings(github_token="ghp_x", github_repo="acme/runbooks"))
    assert tracker.create_issue("Why?", "body") == "https://github.com/acme/runbooks/issues/9"


def test_factory_picks_github_when_jira_absent() -> None:
    from kai.factory import build_providers
    from kai.providers.github_issues import GitHubIssuesTracker as GH

    providers = build_providers(
        Settings(
            _env_file=None,
            embed_base_url="http://x/v1",
            embed_api_key="k",
            embed_model="nomic-embed-text",
            embed_dimensions=768,
            llm_base_url="http://x/v1",
            llm_api_key="k",
            llm_model="m",
            database_url="postgresql://u@localhost/kai",
            source_type="files",
            source_dir=".",
            github_token="t",
            github_repo="acme/kb",
        )
    )
    assert isinstance(providers[4], GH)


# --- URL source ---


def test_html_to_text_skips_script() -> None:
    title, body = _html_to_text(
        "<html><head><title>Doc</title></head>"
        "<body><script>evil()</script><p>Hello world content here</p></body></html>"
    )
    assert title == "Doc"
    assert "Hello world" in body and "evil" not in body


def test_parse_sitemap_urlset() -> None:
    xml = """<?xml version="1.0"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url><loc>https://ex.com/a</loc></url>
      <url><loc>https://ex.com/b</loc></url>
    </urlset>"""
    assert _parse_sitemap(xml, "https://ex.com/sitemap.xml") == [
        "https://ex.com/a",
        "https://ex.com/b",
    ]


def test_url_source_needs_seeds_or_sitemap() -> None:
    with pytest.raises(ValueError, match="SOURCE_URLS"):
        UrlKBSource(_settings(source_urls="", source_sitemap=""))


def test_url_source_yields_docs_from_seeds(monkeypatch) -> None:
    html = (
        "<html><head><title>Runbook</title></head>"
        "<body><p>How to restart the service safely in production.</p></body></html>"
    )

    class _Resp:
        status_code = 200
        text = html

        @property
        def headers(self):
            return {"content-type": "text/html"}

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, follow_redirects=True):
            return _Resp()

    monkeypatch.setattr(httpx, "Client", _Client)
    docs = list(
        UrlKBSource(
            _settings(source_urls="https://ex.com/runbook", source_url_max_pages=5)
        ).iter_pages()
    )
    assert len(docs) == 1
    assert docs[0].title == "Runbook"
    assert docs[0].space == "url"
    assert "restart" in docs[0].html


def test_teams_parse_allows_attachment_only() -> None:
    from kai.chat.teams import parse_activity

    kind, msg, _ = parse_activity(
        {
            "type": "message",
            "text": "",
            "from": {"aadObjectId": "u1", "name": "U"},
            "attachments": [{"contentUrl": "https://ex.com/f.pdf", "name": "f.pdf"}],
        }
    )
    assert kind == "message" and msg is not None

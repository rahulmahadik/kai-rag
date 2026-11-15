"""Unit coverage for 1.2.0 seams: allowlists, notify, GitHub tracker, URL source."""

from __future__ import annotations

import sys
import types

import httpx
import pytest

from kai.chat.access import deny_message, is_allowed
from kai.chat.files import download_url, slack_file_download, teams_attachment_download
from kai.chat.notify import notify_user
from kai.config import Settings
from kai.factory import build_providers
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


def test_notify_rejects_blank_recipient_or_message() -> None:
    ok, _ = notify_user(_settings(webex_bot_token="t"), "", "hi")
    assert not ok
    ok, _ = notify_user(_settings(webex_bot_token="t"), "a@b.co", "  ")
    assert not ok


def test_notify_slack_dm_by_user_id(monkeypatch) -> None:
    class _Client:
        def __init__(self, token):
            self.token = token

        def conversations_open(self, users):
            assert users == "UABC"
            return {"channel": {"id": "D1"}}

        def chat_postMessage(self, channel, text):
            assert channel == "D1" and text == "hi"
            return {"ok": True}

    sdk = types.ModuleType("slack_sdk")
    sdk.WebClient = _Client
    err = types.ModuleType("slack_sdk.errors")

    class _SlackApiError(Exception):
        def __init__(self, message="", response=None):
            super().__init__(message)
            self.response = response

    err.SlackApiError = _SlackApiError
    monkeypatch.setitem(sys.modules, "slack_sdk", sdk)
    monkeypatch.setitem(sys.modules, "slack_sdk.errors", err)

    ok, detail = notify_user(
        _settings(chat_platform="slack", slack_bot_token="xoxb-t"), "UABC", "hi"
    )
    assert ok and detail == "sent"


def test_notify_slack_lookup_by_email_and_api_error(monkeypatch) -> None:
    class _SlackApiError(Exception):
        def __init__(self, message="", response=None):
            super().__init__(message)
            self.response = response

    class _Client:
        def __init__(self, token):
            pass

        def users_lookupByEmail(self, email):
            if email == "bad@x.co":
                raise _SlackApiError("nope", response={"error": "users_not_found"})
            return {"user": {"id": "U9"}}

        def conversations_open(self, users):
            return {"channel": {"id": "D9"}}

        def chat_postMessage(self, channel, text):
            return {"ok": True}

    sdk = types.ModuleType("slack_sdk")
    sdk.WebClient = _Client
    err = types.ModuleType("slack_sdk.errors")
    err.SlackApiError = _SlackApiError
    monkeypatch.setitem(sys.modules, "slack_sdk", sdk)
    monkeypatch.setitem(sys.modules, "slack_sdk.errors", err)

    ok, detail = notify_user(
        _settings(chat_platform="slack", slack_bot_token="xoxb-t"), "ok@x.co", "hi"
    )
    assert ok
    ok, detail = notify_user(
        _settings(chat_platform="slack", slack_bot_token="xoxb-t"), "bad@x.co", "hi"
    )
    assert not ok and "users_not_found" in detail


def test_notify_slack_rejects_opaque_non_id() -> None:
    ok, detail = notify_user(
        _settings(chat_platform="slack", slack_bot_token="xoxb-t"), "not-a-uid", "hi"
    )
    assert not ok and "user id" in detail


# --- file download helpers ---


class _StreamResp:
    def __init__(self, status=200, headers=None, chunks=None):
        self.status_code = status
        self.headers = headers or {}
        self._chunks = chunks or [b"hello"]

    def iter_bytes(self):
        yield from self._chunks

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_download_url_happy_path(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "stream", lambda *a, **k: _StreamResp(chunks=[b"abc", b"def"]))
    got = download_url("https://ex.com/docs/note.pdf")
    assert got == ("note.pdf", b"abcdef")


def test_download_url_empty_and_errors(monkeypatch) -> None:
    assert download_url("") is None
    monkeypatch.setattr(httpx, "stream", lambda *a, **k: _StreamResp(status=404))
    assert download_url("https://ex.com/x") is None

    def _boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(httpx, "stream", _boom)
    assert download_url("https://ex.com/x") is None


def test_download_url_respects_max_bytes(monkeypatch) -> None:
    monkeypatch.setattr(
        httpx,
        "stream",
        lambda *a, **k: _StreamResp(headers={"content-length": "999"}, chunks=[b"x" * 10]),
    )
    assert download_url("https://ex.com/big", max_bytes=5) is None
    monkeypatch.setattr(httpx, "stream", lambda *a, **k: _StreamResp(chunks=[b"12345", b"678"]))
    assert download_url("https://ex.com/big", max_bytes=5) is None


def test_slack_and_teams_download_wrappers(monkeypatch) -> None:
    seen: dict = {}

    def _fake(url, *, headers=None, max_bytes=0, filename_hint=""):
        seen["url"] = url
        seen["headers"] = headers
        seen["hint"] = filename_hint
        return filename_hint or "f", b"data"

    monkeypatch.setattr("kai.chat.files.download_url", _fake)
    assert slack_file_download(
        "tok", {"url_private": "https://files.slack/f", "name": "a.pdf"}
    ) == ("a.pdf", b"data")
    assert seen["headers"]["Authorization"] == "Bearer tok"
    assert teams_attachment_download(
        {"contentUrl": "https://t/f.txt", "name": "f.txt"}, auth_header="Bearer t"
    ) == ("f.txt", b"data")
    assert seen["headers"]["Authorization"] == "Bearer t"


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


def _core_wiring(**over) -> Settings:
    base = {
        "embed_base_url": "http://x/v1",
        "embed_api_key": "k",
        "embed_model": "nomic-embed-text",
        "embed_dimensions": 768,
        "llm_base_url": "http://x/v1",
        "llm_api_key": "k",
        "llm_model": "m",
        "database_url": "postgresql://u@localhost/kai",
        "source_type": "files",
        "source_dir": ".",
    }
    base.update(over)
    return Settings(_env_file=None, **base)


def test_factory_picks_github_when_jira_absent() -> None:
    from kai.providers.github_issues import GitHubIssuesTracker as GH

    providers = build_providers(_core_wiring(github_token="t", github_repo="acme/kb"))
    assert isinstance(providers[4], GH)


def test_factory_builds_url_source() -> None:
    from kai.providers.url_source import UrlKBSource

    kb = build_providers(
        _core_wiring(source_type="urls", source_urls="https://ex.com/a", source_dir="")
    )[3]
    assert isinstance(kb, UrlKBSource)


def test_github_tracker_rejects_nested_repo_path() -> None:
    with pytest.raises(ValueError, match="owner/name"):
        GitHubIssuesTracker(_settings(github_token="t", github_repo="a/b/c"))


def test_github_tracker_truncates_long_titles_and_needs_html_url(monkeypatch) -> None:
    seen: dict = {}

    class _Resp:
        status_code = 201

        def json(self):
            return {}  # missing html_url

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, headers=None, json=None):
            seen["title"] = json["title"]
            return _Resp()

    monkeypatch.setattr(httpx, "Client", _Client)
    tracker = GitHubIssuesTracker(_settings(github_token="t", github_repo="acme/kb"))
    with pytest.raises(RuntimeError, match="html_url"):
        tracker.create_issue("x" * 300, "b")
    assert len(seen["title"]) == 256 and seen["title"].endswith("...")


def test_github_tracker_raises_on_http_error(monkeypatch) -> None:
    class _Resp:
        status_code = 403

        def json(self):
            return {}

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(httpx, "Client", _Client)
    with pytest.raises(RuntimeError, match="status 403"):
        GitHubIssuesTracker(_settings(github_token="t", github_repo="acme/kb")).create_issue(
            "t", "b"
        )


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


def test_url_source_skips_failed_fetches_and_short_bodies(monkeypatch) -> None:
    calls = {"n": 0}

    class _Resp:
        def __init__(self, status, ctype, text):
            self.status_code = status
            self.headers = {"content-type": ctype}
            self.text = text

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, follow_redirects=True):
            calls["n"] += 1
            if "fail" in url:
                return _Resp(500, "text/html", "<html><body>nope</body></html>")
            if "short" in url:
                return _Resp(200, "text/html", "<html><body>tiny</body></html>")
            if "plain" in url:
                return _Resp(
                    200,
                    "text/plain",
                    "This is a long enough plain-text page for ingesting into KAI.",
                )
            raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx, "Client", _Client)
    docs = list(
        UrlKBSource(
            _settings(
                source_urls=(
                    "https://ex.com/fail,https://ex.com/short,"
                    "https://ex.com/plain,https://ex.com/boom"
                ),
                source_url_max_pages=10,
            )
        ).iter_pages()
    )
    assert len(docs) == 1
    assert docs[0].space == "url" and "plain-text" in docs[0].html


def test_url_source_reads_sitemap(monkeypatch) -> None:
    sm = """<?xml version="1.0"?><urlset>
      <url><loc>https://ex.com/page</loc></url></urlset>"""
    page = (
        "<html><head><title>P</title></head>"
        "<body><p>Enough body text for a knowledge page here.</p></body></html>"
    )

    class _Resp:
        def __init__(self, text, ctype="text/html"):
            self.status_code = 200
            self.headers = {"content-type": ctype}
            self.text = text

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, follow_redirects=True):
            if "sitemap" in url:
                return _Resp(sm, "application/xml")
            return _Resp(page)

    monkeypatch.setattr(httpx, "Client", _Client)
    docs = list(
        UrlKBSource(
            _settings(source_sitemap="https://ex.com/sitemap.xml", source_url_max_pages=5)
        ).iter_pages()
    )
    assert len(docs) == 1 and docs[0].title == "P"

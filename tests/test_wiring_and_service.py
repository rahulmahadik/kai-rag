"""Provider wiring, the bot launcher, and ChatService's HTTP calls.

These are the seams every deployment passes through on startup and on every chat
message, and they were the last substantial untested paths: which tracker gets
built, which adapter `CHAT_PLATFORM` selects, and the exact wire format the
inbound-file question travels in.
"""

from __future__ import annotations

import base64
import json

import httpx
import pytest

import kai.bot as bot_module
from kai.chat.base import FeedbackEvent, IncomingMessage
from kai.chat.service import ChatService
from kai.config import Settings
from kai.factory import build_providers
from kai.providers.local_tracker import LocalTracker

_REAL_CLIENT = httpx.Client


def _service(**over) -> ChatService:
    base = {"KAI_API_URL": "http://kai.local", "llm_timeout": 5}
    base.update(over)
    return ChatService(Settings(_env_file=None, **base))


def _patch_post(monkeypatch, handler):
    client = _REAL_CLIENT(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(httpx, "post", client.post)


# ======================================================================= #
# ChatService.answer
# ======================================================================= #
def test_answer_posts_the_question_and_returns_the_payload(monkeypatch) -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"answer": "A.", "escalated": False})

    _patch_post(monkeypatch, handler)
    data, err = _service().answer(IncomingMessage(text="what is x?"))

    assert (data["answer"], err) == ("A.", "")
    assert seen["url"].endswith("/ask")
    assert seen["body"] == {"question": "what is x?"}
    assert seen["auth"] is None, "no key configured means no Authorization header"


def test_the_bot_authenticates_when_an_api_key_is_configured(monkeypatch) -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"answer": "A."})

    _patch_post(monkeypatch, handler)
    _service(KAI_API_KEY="sekret").answer(IncomingMessage(text="q"))

    assert seen["auth"] == "Bearer sekret"


@pytest.mark.parametrize(
    ("status", "fragment"),
    [
        (401, "not configured correctly"),
        (403, "not configured correctly"),
        (422, "try rephrasing"),
        (500, "had a problem"),
        (503, "had a problem"),
    ],
)
def test_answer_maps_each_failure_to_a_useful_message(monkeypatch, status, fragment) -> None:
    """A user must not be told to retry something only an admin can fix."""

    _patch_post(monkeypatch, lambda r: httpx.Response(status, text="detail"))
    data, err = _service().answer(IncomingMessage(text="q"))

    assert data is None
    assert fragment in err


def test_answer_distinguishes_a_timeout_from_an_outage(monkeypatch) -> None:
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    _patch_post(monkeypatch, slow)
    _data, err = _service().answer(IncomingMessage(text="q"))
    assert "longer than expected" in err

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    _patch_post(monkeypatch, down)
    _data, err = _service().answer(IncomingMessage(text="q"))
    assert "couldn't reach" in err


def test_answer_reports_a_malformed_reply(monkeypatch) -> None:
    _patch_post(monkeypatch, lambda r: httpx.Response(200, text="not json"))
    data, err = _service().answer(IncomingMessage(text="q"))
    assert data is None and "malformed" in err


# ======================================================================= #
# ChatService.ask_document
# ======================================================================= #
def test_ask_document_sends_the_file_as_base64(monkeypatch) -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"answer": "About the file."})

    _patch_post(monkeypatch, handler)
    data, err = _service().ask_document("notes.pdf", b"\x00binary\xffbytes", "what is this?")

    assert (data["answer"], err) == ("About the file.", "")
    assert seen["url"].endswith("/ask-document")
    assert seen["body"]["filename"] == "notes.pdf"
    assert seen["body"]["question"] == "what is this?"
    assert base64.b64decode(seen["body"]["content_b64"]) == b"\x00binary\xffbytes"


def test_ask_document_defaults_a_missing_question_and_filename(monkeypatch) -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"answer": "A."})

    _patch_post(monkeypatch, handler)
    _service().ask_document("", b"data", "")

    assert seen["question"] == "Summarize this document."
    assert seen["filename"] == "document"


@pytest.mark.parametrize(
    ("status", "fragment"),
    [(413, "too large"), (500, "couldn't read that file"), (422, "couldn't read that file")],
)
def test_ask_document_maps_failures(monkeypatch, status, fragment) -> None:
    _patch_post(monkeypatch, lambda r: httpx.Response(status))
    data, err = _service().ask_document("f.pdf", b"x", "q")
    assert data is None and fragment in err


def test_ask_document_survives_a_network_error_and_bad_json(monkeypatch) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    _patch_post(monkeypatch, down)
    data, err = _service().ask_document("f.pdf", b"x", "q")
    assert data is None and "couldn't process that file" in err

    _patch_post(monkeypatch, lambda r: httpx.Response(200, text="{{"))
    data, err = _service().ask_document("f.pdf", b"x", "q")
    assert data is None and "malformed" in err


# ======================================================================= #
# ChatService.handle_feedback
# ======================================================================= #
def test_an_escalation_without_a_ticket_url_still_confirms(monkeypatch) -> None:
    """With no external tracker wired there is no link, but the user must still be
    told a human was notified."""

    _patch_post(monkeypatch, lambda r: httpx.Response(200, json={"escalation_url": None}))
    out = _service().handle_feedback(FeedbackEvent(verdict="escalate", question="q"))
    assert out == "Raised for a human to follow up."


def test_an_escalation_with_a_ticket_url_includes_it(monkeypatch) -> None:
    _patch_post(
        monkeypatch, lambda r: httpx.Response(200, json={"escalation_url": "http://jira/K-1"})
    )
    out = _service().handle_feedback(FeedbackEvent(verdict="escalate", question="q"))
    assert "http://jira/K-1" in out


def test_a_failed_feedback_call_never_claims_success(monkeypatch) -> None:
    """The never-fabricate rule applies to the feedback layer too."""

    _patch_post(monkeypatch, lambda r: httpx.Response(500))

    assert "couldn't record" in _service().handle_feedback(
        FeedbackEvent(verdict="up", question="q")
    )
    assert "couldn't raise" in _service().handle_feedback(
        FeedbackEvent(verdict="escalate", question="q")
    )


@pytest.mark.parametrize(("verdict", "fragment"), [("up", "noted"), ("down", "Escalate anyway")])
def test_a_recorded_vote_is_acknowledged(monkeypatch, verdict, fragment) -> None:
    _patch_post(monkeypatch, lambda r: httpx.Response(200, json={"status": "recorded"}))
    assert fragment in _service().handle_feedback(FeedbackEvent(verdict=verdict, question="q"))


def test_an_unknown_verdict_is_ignored() -> None:
    assert _service().handle_feedback(FeedbackEvent(verdict="sideways", question="q")) == ""


# ======================================================================= #
# factory.build_providers
# ======================================================================= #
def _wiring_settings(**over) -> Settings:
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


def test_a_fully_configured_jira_selects_the_real_tracker() -> None:
    from kai.providers.jira_cloud import JiraCloudTracker

    providers = build_providers(
        _wiring_settings(
            jira_base_url="https://acme.atlassian.net",
            jira_email="a@b.co",
            jira_api_token="t",
            jira_project_key="SUP",
        )
    )

    assert isinstance(providers[4], JiraCloudTracker)


@pytest.mark.parametrize(
    "partial",
    [
        {},
        {"jira_base_url": "https://acme.atlassian.net"},
        {"jira_base_url": "https://acme.atlassian.net", "jira_email": "a@b.co"},
        {
            "jira_base_url": "https://acme.atlassian.net",
            "jira_email": "a@b.co",
            "jira_api_token": "t",
        },
    ],
)
def test_partial_jira_config_falls_back_to_the_local_tracker(partial) -> None:
    """Half-configured Jira must not crash startup; it degrades to no-ticket mode."""

    providers = build_providers(_wiring_settings(**partial))
    assert isinstance(providers[4], LocalTracker)


@pytest.mark.parametrize(
    ("blank", "expected"),
    [
        ({"embed_base_url": ""}, "EMBED_BASE_URL"),
        ({"embed_model": ""}, "EMBED_MODEL"),
        ({"llm_base_url": ""}, "LLM_BASE_URL"),
        ({"llm_model": ""}, "LLM_MODEL"),
        ({"database_url": ""}, "DATABASE_URL"),
    ],
)
def test_missing_core_config_fails_loudly_naming_the_variable(blank, expected) -> None:
    with pytest.raises(ValueError, match=expected):
        build_providers(_wiring_settings(**blank))


def test_the_local_tracker_files_nothing_and_returns_no_url() -> None:
    assert LocalTracker().create_issue("title", "body") == ""


# ======================================================================= #
# bot.run_bot
# ======================================================================= #
def test_run_bot_builds_the_configured_adapter_and_runs_it(monkeypatch) -> None:
    ran: list[str] = []

    class _Adapter:
        name = "fake"

        def run(self) -> None:
            ran.append("ran")

    monkeypatch.setattr(bot_module, "get_settings", lambda: _wiring_settings())
    monkeypatch.setattr(bot_module, "build_chat_adapter", lambda s: _Adapter())

    bot_module.run_bot()

    assert ran == ["ran"]


def test_run_bot_propagates_a_startup_failure(monkeypatch) -> None:
    """A misconfigured platform must exit loudly, not appear to start."""

    monkeypatch.setattr(bot_module, "get_settings", lambda: _wiring_settings())

    def boom(_s):
        raise SystemExit("SLACK_BOT_TOKEN is not set")

    monkeypatch.setattr(bot_module, "build_chat_adapter", boom)

    with pytest.raises(SystemExit, match="SLACK_BOT_TOKEN"):
        bot_module.run_bot()


@pytest.mark.parametrize(
    ("platform", "expected"),
    [("webex", "webex"), ("slack", "slack"), ("teams", "teams")],
)
def test_the_chat_platform_setting_selects_the_adapter(platform, expected) -> None:
    from kai.chat import build_chat_adapter

    adapter = build_chat_adapter(_wiring_settings(chat_platform=platform))
    assert adapter.name == expected


def test_an_unknown_chat_platform_is_rejected() -> None:
    from kai.chat import build_chat_adapter

    with pytest.raises((ValueError, SystemExit), match=r"(?i)platform"):
        build_chat_adapter(_wiring_settings(chat_platform="carrier-pigeon"))

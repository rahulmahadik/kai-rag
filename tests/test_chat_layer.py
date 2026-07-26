"""Chat abstraction: service routing + per-platform pure helpers + protocol conformance."""

from __future__ import annotations

import httpx
import pytest

from kai.chat import build_chat_adapter
from kai.chat.base import ChatAdapter, FeedbackEvent, IncomingMessage
from kai.chat.service import ChatService, split_message
from kai.chat.slack import feedback_blocks, md_to_mrkdwn
from kai.chat.webex import feedback_card
from kai.config import Settings


def _settings(**o):
    return Settings(_env_file=None, kai_api_url="http://x", llm_timeout=1, **o)


# ---- portable formatting ----
def test_split_message_respects_limit_and_keeps_all_text():
    text = "\n\n".join(f"Para {i} " + "word " * 50 for i in range(20))
    pieces = split_message(text, limit=600)
    assert all(len(p.encode()) <= 600 for p in pieces)
    assert "Para 0" in pieces[0] and "Para 19" in pieces[-1]


def test_split_short_text_is_single_piece():
    assert split_message("hi", 7000) == ["hi"]


# ---- service: HTTP routing (httpx monkeypatched) ----
class _Resp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._p = payload if payload is not None else {}

    def json(self):
        return self._p

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=None, response=None)


def test_service_answer_ok(monkeypatch):
    monkeypatch.setattr(
        httpx, "post", lambda *a, **k: _Resp(200, {"answer": "hi", "escalated": False})
    )
    data, err = ChatService(_settings()).answer(IncomingMessage(text="q"))
    assert err == "" and data["answer"] == "hi"


def test_service_answer_auth_error_is_admin_message(monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp(401))
    data, err = ChatService(_settings()).answer(IncomingMessage(text="q"))
    assert data is None and "administrator" in err


def test_service_answer_timeout_is_friendly(monkeypatch):
    def _boom(*a, **k):
        raise httpx.TimeoutException("t")

    monkeypatch.setattr(httpx, "post", _boom)
    data, err = ChatService(_settings()).answer(IncomingMessage(text="q"))
    assert data is None and "try again" in err.lower()


def test_service_feedback_routes_escalate(monkeypatch):
    calls = {}

    def _post(url, **k):
        calls["url"] = url
        return _Resp(200, {"escalation_url": "http://t/1"})

    monkeypatch.setattr(httpx, "post", _post)
    msg = ChatService(_settings()).handle_feedback(FeedbackEvent(verdict="escalate", question="q"))
    assert calls["url"].endswith("/escalate") and "t/1" in msg


def test_service_feedback_routes_thumbs(monkeypatch):
    calls = {}
    monkeypatch.setattr(
        httpx, "post", lambda url, **k: (calls.__setitem__("url", url), _Resp(200, {}))[1]
    )
    msg = ChatService(_settings()).handle_feedback(FeedbackEvent(verdict="up", question="q"))
    assert calls["url"].endswith("/feedback") and "noted" in msg.lower()


# ---- slack pure helpers ----
def test_slack_md_to_mrkdwn_converts_links_and_bold():
    out = md_to_mrkdwn("See [RFC 1918](https://x/1) and **bold**.")
    assert "<https://x/1|RFC 1918>" in out
    assert "*bold*" in out and "**" not in out


def test_slack_feedback_blocks_have_three_actions():
    blocks = feedback_blocks("what is x?")
    actions = next(b for b in blocks if b["type"] == "actions")["elements"]
    assert {a["action_id"] for a in actions} == {"kai_fb_up", "kai_fb_down", "kai_fb_escalate"}


def test_collapse_feedback_blocks_removes_buttons_and_confirms():
    from kai.chat.slack import collapse_feedback_blocks

    original = [
        {"type": "section", "text": {"type": "mrkdwn", "text": "answer"}},
        *feedback_blocks("q"),
    ]
    out = collapse_feedback_blocks(original, "✓ noted")
    assert not any(b.get("type") == "actions" for b in out)  # buttons gone (no re-submit)
    assert not any(str(b.get("block_id", "")).startswith("kai_feedback") for b in out)
    assert out[0]["text"]["text"] == "answer"  # original answer preserved
    assert out[-1]["elements"][0]["text"] == "✓ noted"  # confirmation appended


def test_feedback_blocks_escalate_only():
    blocks = feedback_blocks("q", escalate_only=True)
    actions = [b for b in blocks if b["type"] == "actions"]
    assert len(actions) == 1
    assert {e["action_id"] for e in actions[0]["elements"]} == {"kai_fb_escalate"}
    assert actions[0]["block_id"] == "kai_feedback"  # still retired by collapse on tap


def test_format_reply_marks_escalations():
    from kai.chat.service import format_reply

    out = format_reply({"answer": "I couldn't answer this confidently.", "escalated": True})
    assert out.startswith("⚠️")  # glanceable escalation marker on every chat surface
    # a confident answer is NOT marked
    ok = format_reply({"answer": "Yes.", "escalated": False, "citations": []})
    assert not ok.startswith("⚠️")


# ---- webex card ----
def test_webex_feedback_card_is_adaptive():
    card = feedback_card("q")
    assert card["type"] == "AdaptiveCard"
    assert {a["data"]["verdict"] for a in card["actions"]} == {"up", "down", "escalate"}


# ---- protocol conformance + selection ----
def test_adapters_conform_to_protocol():
    assert isinstance(build_chat_adapter(_settings(chat_platform="webex")), ChatAdapter)
    assert isinstance(build_chat_adapter(_settings(chat_platform="slack")), ChatAdapter)


def test_unknown_platform_rejected():
    with pytest.raises(ValueError):
        build_chat_adapter(_settings(chat_platform="zoom"))


def test_slack_swapped_app_token_in_bot_slot_fails_clearly():
    # The common mistake: the App-Level token (xapp-) pasted into SLACK_BOT_TOKEN.
    adapter = build_chat_adapter(
        _settings(
            chat_platform="slack",
            slack_bot_token="xapp-1-A000-111-deadbeef",
            slack_app_token="xapp-1-A000-222-deadbeef",
        )
    )
    with pytest.raises(SystemExit) as exc:
        adapter.run()
    msg = str(exc.value)
    assert "SLACK_BOT_TOKEN must start with 'xoxb-'" in msg
    assert "SLACK_APP_TOKEN" in msg  # tells them where it actually belongs


def test_slack_bot_token_in_app_slot_fails_clearly():
    adapter = build_chat_adapter(
        _settings(
            chat_platform="slack",
            slack_bot_token="xoxb-1-A000-111-deadbeef",
            slack_app_token="xoxb-1-A000-222-deadbeef",
        )
    )
    with pytest.raises(SystemExit) as exc:
        adapter.run()
    msg = str(exc.value)
    assert "SLACK_APP_TOKEN must start with 'xapp-'" in msg
    assert "SLACK_BOT_TOKEN" in msg


def test_slack_start_hint_missing_scope():
    from types import SimpleNamespace

    from kai.chat.slack import _slack_start_hint

    exc = SimpleNamespace(
        response={
            "error": "missing_scope",
            "needed": "connections:write",
            "provided": "app_configurations:write",
        }
    )
    hint = _slack_start_hint(exc)
    assert hint and "connections:write" in hint and "App-Level Token" in hint
    assert "app_configurations:write" in hint  # shows what they actually have


def test_slack_start_hint_invalid_auth_and_unknown():
    from types import SimpleNamespace

    from kai.chat.slack import _slack_start_hint

    assert "invalid_auth" in (
        _slack_start_hint(SimpleNamespace(response={"error": "invalid_auth"})) or ""
    )
    # an unrecognised error returns None so the original traceback is preserved
    assert _slack_start_hint(SimpleNamespace(response={"error": "weird_error"})) is None


def test_teams_platform_builds_adapter():
    # Teams is now a real adapter (no longer raises); construction needs no Azure.
    from kai.chat.teams import TeamsAdapter

    assert isinstance(build_chat_adapter(_settings(chat_platform="teams")), TeamsAdapter)


# ---- Webex REST helpers (edit-in-place + DM), httpx mocked ----
def test_webex_create_edit_dm(monkeypatch):
    import httpx as _httpx

    from kai.chat import webex as wx

    calls = {}

    class _R:
        def __init__(self, status, payload=None):
            self.status_code = status
            self._p = payload or {}

        def json(self):
            return self._p

    def _post(url, json=None, headers=None, timeout=None):
        calls["post"] = (url, json)
        return _R(200, {"id": "MSG123"})

    def _put(url, json=None, headers=None, timeout=None):
        calls["put"] = (url, json)
        return _R(200)

    monkeypatch.setattr(_httpx, "post", _post)
    monkeypatch.setattr(_httpx, "put", _put)

    mid = wx.create_message("tok", roomId="R1", parentId="P1", markdown="hi")
    assert mid == "MSG123" and calls["post"][1]["roomId"] == "R1"
    assert wx.edit_message("tok", "MSG123", "R1", "answer") is True
    assert calls["put"][0].endswith("/MSG123") and calls["put"][1]["markdown"] == "answer"
    assert wx.send_direct_message("tok", "a@b.c", "ping") is True
    assert calls["post"][1]["toPersonEmail"] == "a@b.c"


def test_webex_helpers_never_raise_on_error(monkeypatch):
    import httpx as _httpx

    from kai.chat import webex as wx

    def _boom(*a, **k):
        raise _httpx.HTTPError("down")

    monkeypatch.setattr(_httpx, "post", _boom)
    monkeypatch.setattr(_httpx, "put", _boom)
    assert wx.create_message("t", roomId="R", markdown="x") is None
    assert wx.edit_message("t", "M", "R", "x") is False
    assert wx.send_direct_message("t", "a@b.c", "x") is False

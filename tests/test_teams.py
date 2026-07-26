"""Microsoft Teams adapter, pure parsing/routing helpers (no Azure/network).

The live Connector reply + Bot-Framework-auth round-trip needs a real Azure tenant
and a public endpoint (verified in-tenant). These tests pin the parsing that decides
message-vs-feedback-vs-ignore and the reply-activity shape."""

from __future__ import annotations

import pytest

from kai.chat import build_chat_adapter
from kai.chat.teams import TeamsAdapter, _strip_mention, build_reply_activity, parse_activity
from kai.config import Settings


def test_teams_refuses_inbound_when_unconfigured():
    # No TEAMS_APP_ID => cannot verify the caller => refuse (don't accept unauth'd
    # activities that could drive replies / leak a connector token).
    adapter = TeamsAdapter(Settings(_env_file=None, kai_api_url="http://x"))
    with pytest.raises(RuntimeError, match="not configured"):
        adapter._validate("Bearer anything")


def test_strip_mention():
    assert _strip_mention("<at>KAI</at> what is replication?") == "what is replication?"
    assert _strip_mention("no mention here") == "no mention here"


def test_parse_message_routes_to_ask():
    act = {"type": "message", "text": "<at>KAI</at> what is x?", "from": {"id": "u1"}}
    kind, msg, fb = parse_activity(act)
    assert kind == "message" and fb is None
    assert msg.text == "what is x?" and msg.sender_email == "u1"


def test_parse_feedback_card_submit():
    act = {
        "type": "message",
        "from": {"aadObjectId": "u9"},
        "value": {"callback_keyword": "kai_feedback", "verdict": "down", "question": "what is x?"},
    }
    kind, msg, fb = parse_activity(act)
    assert kind == "feedback" and msg is None
    assert fb.verdict == "down" and fb.question == "what is x?" and fb.sender_email == "u9"


def test_parse_empty_and_nonmessage_ignored():
    assert parse_activity({"type": "message", "text": "<at>KAI</at>   ", "from": {}})[0] == "ignore"
    assert parse_activity({"type": "conversationUpdate"})[0] == "ignore"


def test_build_reply_activity_text_and_card():
    incoming = {
        "id": "m1",
        "from": {"id": "u1"},
        "recipient": {"id": "bot"},
        "conversation": {"id": "c1"},
    }
    r = build_reply_activity(incoming, "hello")
    assert r["type"] == "message" and r["text"] == "hello"
    assert r["from"] == {"id": "bot"} and r["recipient"] == {"id": "u1"}
    assert r["conversation"] == {"id": "c1"} and r["replyToId"] == "m1"
    assert "attachments" not in r
    r2 = build_reply_activity(incoming, "hi", card={"type": "AdaptiveCard"})
    assert r2["attachments"][0]["contentType"] == "application/vnd.microsoft.card.adaptive"


def test_build_chat_adapter_returns_teams():
    a = build_chat_adapter(Settings(_env_file=None, chat_platform="teams"))
    assert isinstance(a, TeamsAdapter) and a.name == "teams"

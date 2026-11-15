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


def test_teams_webhook_remembers_conversation(monkeypatch):
    from fastapi.testclient import TestClient

    captured: dict = {}
    remembered: list = []

    def _fake_uvicorn(app, **_kw):
        captured["app"] = app

    monkeypatch.setattr("uvicorn.run", _fake_uvicorn)
    monkeypatch.setattr(
        "kai.chat.teams_refs.TeamsConversationStore.remember",
        lambda self, act: remembered.append(act) or True,
    )
    adapter = TeamsAdapter(
        Settings(
            _env_file=None,
            kai_api_url="http://x",
            teams_app_id="app",
            teams_app_password="pw",
            database_url="postgresql://u@localhost/kai",
        )
    )
    monkeypatch.setattr(adapter, "_validate", lambda *a, **k: None)
    monkeypatch.setattr(adapter, "_reply", lambda *a, **k: None)
    monkeypatch.setattr(
        adapter._service, "answer", lambda msg: ({"answer": "ok", "escalated": False}, "")
    )
    adapter.run()
    client = TestClient(captured["app"], raise_server_exceptions=False)
    r = client.post(
        "/api/messages",
        json={
            "type": "message",
            "text": "hello",
            "serviceUrl": "https://smba.example/",
            "from": {"id": "29:u", "aadObjectId": "aad"},
            "recipient": {"id": "28:bot"},
            "conversation": {"id": "a:1"},
        },
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    assert remembered and remembered[0]["conversation"]["id"] == "a:1"


def test_teams_webhook_allowlist_and_file_attach(monkeypatch):
    """Capture the FastAPI app from run() and exercise allowlist + attachment paths."""

    from fastapi.testclient import TestClient

    captured: dict = {}

    def _fake_uvicorn(app, **_kw):
        captured["app"] = app

    monkeypatch.setattr("uvicorn.run", _fake_uvicorn)
    adapter = TeamsAdapter(
        Settings(
            _env_file=None,
            kai_api_url="http://x",
            teams_app_id="app",
            teams_app_password="pw",
            teams_approved_users="allowed-user",
        )
    )
    monkeypatch.setattr(adapter, "_validate", lambda *a, **k: None)
    replies: list = []
    monkeypatch.setattr(adapter, "_reply", lambda act, text, card=None: replies.append(text))
    adapter.run()
    client = TestClient(captured["app"], raise_server_exceptions=False)

    # Denied by allowlist.
    r = client.post(
        "/api/messages",
        json={"type": "message", "text": "hello", "from": {"aadObjectId": "other"}},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    assert any("allowlist" in t.lower() for t in replies)

    # Attachment → ask_document.
    replies.clear()
    monkeypatch.setattr(
        "kai.chat.teams.teams_attachment_download",
        lambda *a, **k: ("doc.pdf", b"%PDF"),
    )
    monkeypatch.setattr(
        adapter._service,
        "ask_document",
        lambda name, data, q: ({"answer": "from file", "escalated": False}, ""),
    )
    r = client.post(
        "/api/messages",
        json={
            "type": "message",
            "text": "summarize",
            "from": {"aadObjectId": "allowed-user"},
            "attachments": [{"contentUrl": "https://ex/f", "name": "doc.pdf"}],
        },
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    assert any("doc.pdf" in t and "knowledge base" in t for t in replies)

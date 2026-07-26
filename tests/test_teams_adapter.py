"""The Teams adapter's webhook, auth, and Connector calls.

Teams is the one inbound surface: Azure POSTs activities to an endpoint we host, so
the auth check on that endpoint is load-bearing. `run()` builds a FastAPI app and
hands it to uvicorn; patching `uvicorn.run` captures the app so TestClient can drive
the real route.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from kai.chat.teams import TeamsAdapter, build_reply_activity, parse_activity
from kai.config import Settings

_REAL_CLIENT = httpx.Client


def _settings(**over) -> Settings:
    base = {
        "teams_app_id": "app-123",
        "teams_app_password": "secret",
        "kai_api_url": "http://kai.local",
        "llm_timeout": 5,
    }
    base.update(over)
    return Settings(_env_file=None, **base)


def _activity(**over) -> dict:
    base = {
        "type": "message",
        "id": "act-1",
        "text": "How do I rotate keys?",
        "serviceUrl": "https://smba.example.com/",
        "conversation": {"id": "conv-1"},
        "from": {"id": "user-1", "aadObjectId": "aad-1"},
        "recipient": {"id": "bot-1"},
    }
    base.update(over)
    return base


@pytest.fixture
def adapter_app(monkeypatch):
    """The real /api/messages app, with auth stubbed to accept and uvicorn no-op'd."""

    adapter = TeamsAdapter(_settings())
    captured: dict = {}

    monkeypatch.setattr("uvicorn.run", lambda app, **kw: captured.update(app=app, kw=kw))
    monkeypatch.setattr(adapter, "_validate", lambda auth, expected_service_url="": None)
    adapter.run()

    return adapter, TestClient(captured["app"], raise_server_exceptions=False), captured


# ======================================================================= #
# parse_activity
# ======================================================================= #
def test_a_message_activity_becomes_an_incoming_message() -> None:
    kind, msg, fb = parse_activity(_activity(text="<at>KAI</at> what is x?"))

    assert kind == "message"
    assert msg.text == "what is x?", "the bot mention must be stripped"
    assert msg.sender_email == "aad-1"
    assert fb is None


def test_a_card_submit_becomes_a_feedback_event() -> None:
    activity = _activity(
        text="",
        value={"callback_keyword": "kai_feedback", "verdict": "down", "question": "q"},
    )

    kind, msg, fb = parse_activity(activity)

    assert kind == "feedback"
    assert (fb.verdict, fb.question, fb.sender_email) == ("down", "q", "aad-1")
    assert msg is None


@pytest.mark.parametrize(
    "activity",
    [
        {"type": "conversationUpdate"},
        {"type": "message", "text": "   "},
        {"type": "message", "text": "<at>KAI</at>"},
        {"type": "typing"},
    ],
)
def test_activities_with_nothing_to_answer_are_ignored(activity) -> None:
    assert parse_activity(activity)[0] == "ignore"


def test_a_value_that_is_not_a_feedback_payload_is_treated_as_a_message() -> None:
    kind, msg, _fb = parse_activity(_activity(value={"something": "else"}))
    assert kind == "message" and msg.text


def test_build_reply_activity_swaps_the_participants_and_threads() -> None:
    reply = build_reply_activity(_activity(), "hello")

    assert reply["from"] == {"id": "bot-1"}
    assert reply["recipient"] == {"id": "user-1", "aadObjectId": "aad-1"}
    assert reply["conversation"] == {"id": "conv-1"}
    assert reply["replyToId"] == "act-1"
    assert reply["textFormat"] == "markdown"
    assert "attachments" not in reply


def test_build_reply_activity_attaches_an_adaptive_card() -> None:
    reply = build_reply_activity(_activity(), "hi", {"type": "AdaptiveCard"})
    att = reply["attachments"][0]
    assert att["contentType"] == "application/vnd.microsoft.card.adaptive"
    assert att["content"] == {"type": "AdaptiveCard"}


# ======================================================================= #
# Inbound auth
# ======================================================================= #
def test_an_unconfigured_bot_refuses_every_request() -> None:
    """An open webhook could be driven to reply, leaking a connector token to an
    attacker-supplied serviceUrl."""

    adapter = TeamsAdapter(_settings(teams_app_id=""))

    with pytest.raises(RuntimeError, match="TEAMS_APP_ID"):
        adapter._validate("Bearer whatever")


def test_the_webhook_returns_401_when_validation_fails(monkeypatch) -> None:
    adapter = TeamsAdapter(_settings())
    captured: dict = {}
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: captured.update(app=app))

    def reject(auth, expected_service_url=""):
        raise RuntimeError("bad token")

    monkeypatch.setattr(adapter, "_validate", reject)
    adapter.run()
    client = TestClient(captured["app"], raise_server_exceptions=False)

    resp = client.post("/api/messages", json=_activity())

    assert resp.status_code == 401


def test_a_service_url_that_does_not_match_the_token_is_refused(monkeypatch) -> None:
    """A signed token must not be able to redirect our reply, and its bearer token,
    to another host."""

    adapter = TeamsAdapter(_settings())

    class _Key:
        key = "k"

    class _JWKS:
        def get_signing_key_from_jwt(self, token):
            return _Key()

    import jwt

    monkeypatch.setattr(jwt, "PyJWKClient", lambda url: _JWKS())
    monkeypatch.setattr(jwt, "decode", lambda *a, **k: {"serviceurl": "https://legit.example.com"})

    adapter._validate("Bearer t", expected_service_url="https://legit.example.com/")
    with pytest.raises(RuntimeError, match="serviceUrl"):
        adapter._validate("Bearer t", expected_service_url="https://attacker.example.com")


# ======================================================================= #
# The webhook route
# ======================================================================= #
def test_a_question_is_answered_through_the_connector(adapter_app, monkeypatch) -> None:
    adapter, client, _ = adapter_app
    sent: list[dict] = []
    monkeypatch.setattr(adapter._service, "answer", lambda msg: ({"answer": "A [1]."}, ""))
    monkeypatch.setattr(adapter, "_reply", lambda a, t, c: sent.append({"text": t, "card": c}))

    resp = client.post("/api/messages", json=_activity())

    assert resp.status_code == 200
    assert sent and "A [1]." in sent[0]["text"]


def test_help_is_answered_without_calling_the_api(adapter_app, monkeypatch) -> None:
    adapter, client, _ = adapter_app
    calls: list = []
    sent: list[str] = []
    monkeypatch.setattr(adapter._service, "answer", lambda msg: calls.append(msg) or (None, "x"))
    monkeypatch.setattr(adapter, "_reply", lambda a, t, c: sent.append(t))

    client.post("/api/messages", json=_activity(text="help"))

    assert calls == []
    assert "I'm KAI" in sent[0]


def test_an_api_failure_is_reported_to_the_user(adapter_app, monkeypatch) -> None:
    adapter, client, _ = adapter_app
    sent: list[str] = []
    monkeypatch.setattr(adapter._service, "answer", lambda msg: (None, "Sorry, KAI is down."))
    monkeypatch.setattr(adapter, "_reply", lambda a, t, c: sent.append(t))

    client.post("/api/messages", json=_activity())

    assert sent == ["Sorry, KAI is down."]


def test_a_feedback_tap_is_routed_and_retires_the_card(adapter_app, monkeypatch) -> None:
    """Teams keeps Action.Submit buttons live, so the card must be replaced or a
    user can file duplicate escalations."""

    adapter, client, _ = adapter_app
    retired: list[str] = []
    monkeypatch.setattr(adapter._service, "handle_feedback", lambda fb: "Thanks, noted.")
    monkeypatch.setattr(adapter, "_retire_card", lambda a, t: retired.append(t))

    resp = client.post(
        "/api/messages",
        json=_activity(
            text="",
            value={"callback_keyword": "kai_feedback", "verdict": "up", "question": "q"},
        ),
    )

    assert resp.status_code == 200
    assert retired == ["Thanks, noted."]


def test_an_ignored_activity_is_acknowledged_quietly(adapter_app, monkeypatch) -> None:
    adapter, client, _ = adapter_app
    sent: list = []
    monkeypatch.setattr(adapter, "_reply", lambda a, t, c: sent.append(t))

    resp = client.post("/api/messages", json={"type": "conversationUpdate"})

    assert resp.status_code == 200
    assert sent == []


# ======================================================================= #
# Connector token + outbound reply
# ======================================================================= #
def _patch_httpx(monkeypatch, handler) -> None:
    client = _REAL_CLIENT(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(httpx, "post", client.post)
    monkeypatch.setattr(httpx, "put", client.put)


def test_the_connector_token_is_cached_between_replies(monkeypatch) -> None:
    adapter = TeamsAdapter(_settings())
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})

    _patch_httpx(monkeypatch, handler)

    assert adapter._connector_token() == "tok-1"
    assert adapter._connector_token() == "tok-1"
    assert len(calls) == 1, "a still-valid token must not be re-fetched"


def test_an_expired_token_is_refetched(monkeypatch) -> None:
    adapter = TeamsAdapter(_settings())
    tokens = ["tok-1", "tok-2"]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": tokens.pop(0), "expires_in": 0})

    _patch_httpx(monkeypatch, handler)

    assert adapter._connector_token() == "tok-1"
    assert adapter._connector_token() == "tok-2"


def test_reply_posts_each_piece_to_the_conversation(monkeypatch) -> None:
    adapter = TeamsAdapter(_settings())
    posted: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        if "oauth2" in str(request.url):
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        posted.append({"url": str(request.url), "body": json.loads(request.content)})
        return httpx.Response(200, json={"id": "m"})

    _patch_httpx(monkeypatch, handler)
    adapter._reply(_activity(), "hello there", None)

    assert len(posted) == 1
    assert posted[0]["url"].endswith("/v3/conversations/conv-1/activities")
    assert posted[0]["body"]["text"] == "hello there"


def test_reply_is_skipped_when_the_activity_has_no_conversation(monkeypatch) -> None:
    adapter = TeamsAdapter(_settings())
    calls: list = []
    _patch_httpx(monkeypatch, lambda r: calls.append(1) or httpx.Response(200, json={}))

    adapter._reply({"serviceUrl": "", "conversation": {}}, "hi", None)

    assert calls == []


def test_a_failed_token_fetch_sends_nothing(monkeypatch) -> None:
    adapter = TeamsAdapter(_settings())
    posted: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "oauth2" in str(request.url):
            return httpx.Response(401, json={"error": "bad creds"})
        posted.append(1)
        return httpx.Response(200, json={})

    _patch_httpx(monkeypatch, handler)
    adapter._reply(_activity(), "hi", None)

    assert posted == [], "no token means nothing should be sent"


def test_a_truncated_multi_part_reply_tells_the_user(monkeypatch) -> None:
    """A mid-stream failure would otherwise leave a silent partial answer that
    looks complete."""

    adapter = TeamsAdapter(_settings())
    bodies: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        if "oauth2" in str(request.url):
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        text = json.loads(request.content).get("text", "")
        bodies.append(text)
        # Fail the second content piece, then accept the apology.
        if len(bodies) == 2 and "cut off" not in text:
            return httpx.Response(502)
        return httpx.Response(200, json={"id": "m"})

    _patch_httpx(monkeypatch, handler)
    # Comfortably over the 25,000-char Teams split limit, so this becomes >1 piece.
    body = "\n\n".join("para " + "w " * 200 for _ in range(100))
    assert len(body) > 25_000
    adapter._reply(_activity(), body, None)

    assert any("cut off" in b for b in bodies)


def test_retire_card_updates_the_original_message(monkeypatch) -> None:
    adapter = TeamsAdapter(_settings())
    puts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "oauth2" in str(request.url):
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        if request.method == "PUT":
            puts.append(str(request.url))
            return httpx.Response(200, json={})
        return httpx.Response(200, json={"id": "m"})

    _patch_httpx(monkeypatch, handler)
    adapter._retire_card(_activity(replyToId="card-9"), "Thanks.")

    assert puts and puts[0].endswith("/v3/conversations/conv-1/activities/card-9")


def test_retire_card_falls_back_to_a_plain_reply_without_a_card_id(monkeypatch) -> None:
    adapter = TeamsAdapter(_settings())
    sent: list[str] = []
    monkeypatch.setattr(adapter, "_reply", lambda a, t, c: sent.append(t))

    adapter._retire_card(_activity(), "Thanks.")

    assert sent == ["Thanks."]


def test_retire_card_falls_back_to_a_reply_when_the_update_fails(monkeypatch) -> None:
    """The confirmation still has to reach the user even if the card cannot be
    replaced, or a tap looks like it did nothing."""

    adapter = TeamsAdapter(_settings())
    replied: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "oauth2" in str(request.url):
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        return httpx.Response(500)

    _patch_httpx(monkeypatch, handler)
    monkeypatch.setattr(adapter, "_reply", lambda a, t, c: replied.append(t))

    adapter._retire_card(_activity(replyToId="card-9"), "Thanks.")

    assert replied == ["Thanks."]


def test_retire_card_sends_nothing_when_the_token_fetch_fails(monkeypatch) -> None:
    adapter = TeamsAdapter(_settings())
    replied: list[str] = []

    _patch_httpx(monkeypatch, lambda r: httpx.Response(401, json={"error": "bad"}))
    monkeypatch.setattr(adapter, "_reply", lambda a, t, c: replied.append(t))

    adapter._retire_card(_activity(replyToId="card-9"), "Thanks.")

    assert replied == [], "without a token there is nothing to send"

"""API-level tests: auth (Q1), generic 500 (Q2), blank-422, feedback/escalate (M4),
/metrics (M3), with stubbed providers (no network/DB/models)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import kai.app as app_module
from kai.config import Settings
from kai.interfaces import Answer, Citation


class _FakeTracker:
    def create_issue(self, title: str, body: str) -> str:
        return "http://jira/KAI-9"


def _fake_providers(*_a, **_k):
    return (object(), object(), object(), object(), _FakeTracker())


def _answer(**over):
    base = {
        "answer": "Replication copies data [1].",
        "citations": [Citation(title="Replication", url="http://kb/r")],
        "confidence": 0.9,
        "escalated": False,
        "escalation_url": None,
    }
    base.update(over)
    return Answer(**base)


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(app_module, "build_providers", _fake_providers)
    monkeypatch.setattr(app_module, "ask_pipeline", lambda q, p, s: _answer())
    settings = Settings(
        _env_file=None,  # hermetic: don't absorb the local .env
        KAI_API_KEY="sekret",  # field uses a validation_alias, kwarg must match it
        reranker="noop",
        database_url="",
        answer_cache_size=0,
    )
    app = app_module.create_app(settings)
    return TestClient(app, raise_server_exceptions=False)


AUTH = {"Authorization": "Bearer sekret"}


def test_health_open_no_auth(client):
    assert client.get("/health").status_code == 200


def test_ask_requires_bearer(client):
    assert client.post("/ask", json={"question": "x"}).status_code == 401
    bad = client.post("/ask", json={"question": "x"}, headers={"Authorization": "Bearer wrong"})
    assert bad.status_code == 401


def test_blank_question_is_422(client):
    for q in ("", "   ", "\n\t"):
        r = client.post("/ask", json={"question": q}, headers=AUTH)
        assert r.status_code == 422, q


def test_ask_happy_path_includes_suggested_sources_field(client):
    r = client.post("/ask", json={"question": "how does replication work"}, headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["answer"].startswith("Replication")
    assert body["citations"][0]["url"] == "http://kb/r"
    assert "suggested_sources" in body  # additive API field for escalations


def test_unhandled_error_returns_generic_500(monkeypatch, client):
    def _boom(q, p, s):
        raise RuntimeError("secret-internal-url http://10.0.0.1:11434")

    monkeypatch.setattr(app_module, "ask_pipeline", _boom)
    r = client.post("/ask", json={"question": "x"}, headers=AUTH)
    assert r.status_code == 500
    assert "secret-internal-url" not in r.text  # Q2: nothing internal leaks


def test_feedback_records(client):
    r = client.post(
        "/feedback",
        json={"question": "q", "verdict": "down", "reporter": "a@b.c"},
        headers=AUTH,
    )
    assert r.status_code == 200 and r.json()["status"] == "recorded"
    bad = client.post("/feedback", json={"question": "q", "verdict": "maybe"}, headers=AUTH)
    assert bad.status_code == 422  # verdict restricted to up|down


def test_escalate_files_ticket_without_rerunning_ask(client):
    r = client.post("/escalate", json={"question": "q", "reporter": "a@b.c"}, headers=AUTH)
    assert r.status_code == 200
    assert r.json()["escalation_url"] == "http://jira/KAI-9"


def test_metrics_exposed_as_text(client):
    client.post("/ask", json={"question": "warm the counters"}, headers=AUTH)
    r = client.get("/metrics", headers=AUTH)  # /metrics is auth-gated when a key is set
    assert r.status_code == 200
    assert "kai_asks_total" in r.text


def test_metrics_requires_auth_when_keyed(client):
    assert client.get("/metrics").status_code == 401


def test_oversized_question_rejected_422(client):
    r = client.post("/ask", json={"question": "x" * 5000}, headers=AUTH)
    assert r.status_code == 422  # max_length guard fires before any embed/LLM work


def test_notify_requires_webex_token(client):
    # the test settings have no WEBEX_BOT_TOKEN -> 400, not a crash
    r = client.post("/notify", json={"email": "a@b.c", "message": "hi"}, headers=AUTH)
    assert r.status_code == 400


def test_notify_sends_when_token_present(monkeypatch):
    import kai.app as app_module
    from kai.config import Settings

    monkeypatch.setattr(app_module, "build_providers", _fake_providers)
    monkeypatch.setattr("kai.chat.webex.send_direct_message", lambda *a, **k: True)
    s = Settings(
        _env_file=None,
        KAI_API_KEY="sekret",
        WEBEX_BOT_TOKEN="tok",
        reranker="noop",
        database_url="",
        answer_cache_size=0,
    )
    c = TestClient(app_module.create_app(s), raise_server_exceptions=False)
    r = c.post(
        "/notify",
        json={"email": "a@b.c", "message": "done"},
        headers={"Authorization": "Bearer sekret"},
    )
    assert r.status_code == 200 and r.json()["status"] == "sent"


def _notify_client(monkeypatch, send_result):
    import kai.app as app_module
    from kai.config import Settings

    monkeypatch.setattr(app_module, "build_providers", _fake_providers)
    monkeypatch.setattr("kai.chat.webex.send_direct_message", lambda *a, **k: send_result)
    s = Settings(
        _env_file=None,
        KAI_API_KEY="sekret",
        WEBEX_BOT_TOKEN="tok",
        reranker="noop",
        database_url="",
        answer_cache_size=0,
    )
    return TestClient(app_module.create_app(s), raise_server_exceptions=False)


def test_notify_failed_send_returns_502(monkeypatch):
    # A failed DM must NOT return 200. The caller has to know it didn't land.
    c = _notify_client(monkeypatch, send_result=False)
    r = c.post("/notify", json={"email": "a@b.c", "message": "x"}, headers=AUTH)
    assert r.status_code == 502


def test_notify_invalid_email_422(monkeypatch):
    c = _notify_client(monkeypatch, send_result=True)
    r = c.post("/notify", json={"email": "not-an-email", "message": "x"}, headers=AUTH)
    assert r.status_code == 422


def test_admin_inform_rejects_unknown_status(client):
    # An unknown status must 422 (not silently return everything).
    assert client.get("/admin/inform?status=bogus", headers=AUTH).status_code == 422


def test_feedback_down_survives_quarantine_db_error(client, monkeypatch):
    # The quarantine path touches the DB; if it fails, /feedback must still 200
    # (the 👎 was already recorded): never break /feedback.
    from kai.pipeline.inform import InformStore

    def _boom(self, q):
        raise RuntimeError("db down")

    monkeypatch.setattr(InformStore, "downvote_curated", _boom)
    r = client.post("/feedback", json={"question": "q", "verdict": "down"}, headers=AUTH)
    assert r.status_code == 200 and r.json()["status"] == "recorded"


def test_home_returns_api_banner(client):
    # The web UI is a separate app (frontend/); "/" is an API banner, not HTML.
    r = client.get("/")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "KAI" and body["health"] == "/health"
    # the fixture sets an API key, so docs are gated off (not advertised)
    assert body["docs"] is None
    # in keyed/prod mode the banner does NOT enumerate the API surface to
    # unauthenticated callers (the endpoint map is omitted)
    assert "endpoints" not in body


def test_reindex_endpoint_gated_and_wires_inform(client, monkeypatch):
    captured = {}

    def fake_reindex(providers, **kw):
        captured.update(kw)
        return {"chunks": 12, "curated": 3}

    monkeypatch.setattr("kai.pipeline.ingest.reindex", fake_reindex)

    assert client.post("/admin/reindex").status_code == 401  # API-key gated
    r = client.post("/admin/reindex", headers=AUTH)
    assert r.status_code == 200
    assert r.json() == {"chunks": 12, "curated": 3}
    # the endpoint passes the Inform queue through so curated answers get rebuilt
    assert captured["inform_store"] is not None


def _cors_client(monkeypatch, cors):
    monkeypatch.setattr(app_module, "build_providers", _fake_providers)
    settings = Settings(
        _env_file=None,
        KAI_API_KEY="",
        reranker="noop",
        database_url="",
        answer_cache_size=0,
        cors_origins=cors,
    )
    return TestClient(app_module.create_app(settings), raise_server_exceptions=False)


def test_cors_denied_by_default(monkeypatch):
    c = _cors_client(monkeypatch, "")  # no CORS_ORIGINS -> no cross-origin access
    r = c.get("/health", headers={"Origin": "https://evil.com"})
    assert "access-control-allow-origin" not in {k.lower() for k in r.headers}


def test_cors_allowlist_echoes_only_allowed_origin(monkeypatch):
    c = _cors_client(monkeypatch, "https://app.example.com")
    ok = c.get("/health", headers={"Origin": "https://app.example.com"})
    assert ok.headers.get("access-control-allow-origin") == "https://app.example.com"
    bad = c.get("/health", headers={"Origin": "https://evil.com"})
    assert bad.headers.get("access-control-allow-origin") is None


def test_search_endpoint_auth_shape_and_maxlen(monkeypatch, client):
    monkeypatch.setattr("kai.pipeline.ask.retrieve", lambda q, *a, **k: (q, []))
    assert client.post("/search", json={"question": "x"}).status_code == 401  # gated
    r = client.post("/search", json={"question": "kafka"}, headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert "query" in body and "confidence" in body
    assert client.post("/search", json={"question": "x" * 5000}, headers=AUTH).status_code == 422


def test_docs_disabled_when_keyed(client):
    # the fixture sets an API key -> interactive docs + schema are gated off
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_ask_document_no_readable_text_escalates(client):
    import base64

    b64 = base64.b64encode(b"   ").decode()  # a "file" with no extractable text
    r = client.post(
        "/ask-document",
        json={"question": "what is x?", "filename": "x.txt", "content_b64": b64},
        headers=AUTH,
    )
    assert r.status_code == 200  # not a 500 NameError (Answer now imported)
    assert r.json()["escalated"] is True

"""The admin surface: ingest, reindex, gaps, notify, and the Inform approval loop.

The load-bearing rule here is structural: nothing reaches the vector store until
someone approves it. These cases drive that loop through the HTTP layer with an
in-memory Inform queue, so the state machine and its guards are exercised without
a database.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import kai.app as app_module
from kai.config import Settings
from kai.interfaces import Answer, Citation

AUTH = {"Authorization": "Bearer sekret"}


class FakeStore:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    def delete(self, doc_id: str) -> None:
        self.deleted.append(doc_id)


class FakeTracker:
    def __init__(self) -> None:
        self.issues: list[tuple[str, str]] = []

    def create_issue(self, title: str, body: str) -> str:
        self.issues.append((title, body))
        return "http://jira/KAI-9"


class FakeInform:
    """In-memory stand-in for InformStore with the same surface the app uses."""

    def __init__(self) -> None:
        self.rows: dict[int, dict] = {}
        self.next_id = 1
        self.downvote_hits: list[dict] = []

    def submit(self, question, answer, author="", asker="") -> int:
        cid = self.next_id
        self.next_id += 1
        self.rows[cid] = {
            "id": cid,
            "question": question,
            "answer": answer,
            "author": author,
            "asker": asker,
            "approver": "",
            "status": "pending",
            "chunks": 0,
            "downvotes": 0,
        }
        return cid

    def get(self, cid):
        return self.rows.get(cid)

    def list(self, status="pending", limit=100, offset=0):
        rows = [r for r in self.rows.values() if not status or r["status"] == status]
        return rows[offset : offset + limit]

    def mark(self, cid, status, chunks=None, approver=None, downvotes=None) -> None:
        row = self.rows[cid]
        row["status"] = status
        if chunks is not None:
            row["chunks"] = chunks
        if approver is not None:
            row["approver"] = approver
        if downvotes is not None:
            row["downvotes"] = downvotes

    def downvote_curated(self, question):
        return self.downvote_hits


class FakeTelemetry:
    def __init__(self, *a, **k) -> None:
        self.asks: list[tuple] = []
        self.feedback: list[tuple] = []
        self.gap_rows: list[dict] = []
        self.gap_limit: int | None = None

    def record_ask(self, question, **kw) -> None:
        self.asks.append((question, kw))

    def record_cache_hit(self) -> None:
        self.asks.append(("<cache>", {}))

    def record_feedback(self, question, verdict, reporter) -> None:
        self.feedback.append((question, verdict, reporter))

    def metrics_text(self) -> str:
        return "kai_asks_total 1\n"

    def gaps(self, limit=50):
        self.gap_limit = limit
        return self.gap_rows


@pytest.fixture
def ctx(monkeypatch):
    """A TestClient plus the fakes the handlers were wired with."""

    store, tracker, inform = FakeStore(), FakeTracker(), FakeInform()
    telemetry = FakeTelemetry()

    monkeypatch.setattr(
        app_module,
        "build_providers",
        lambda *_a, **_k: (object(), object(), store, object(), tracker),
    )
    monkeypatch.setattr(app_module, "ask_pipeline", lambda q, p, s: _answer())
    monkeypatch.setattr("kai.telemetry.Telemetry", lambda *a, **k: telemetry)
    monkeypatch.setattr("kai.pipeline.inform.InformStore", lambda *a, **k: inform)

    settings = Settings(
        _env_file=None,
        KAI_API_KEY="sekret",
        reranker="noop",
        database_url="",
        answer_cache_size=8,
    )
    client = TestClient(app_module.create_app(settings), raise_server_exceptions=False)
    return {
        "client": client,
        "store": store,
        "tracker": tracker,
        "inform": inform,
        "telemetry": telemetry,
    }


def _answer(**over) -> Answer:
    base = {
        "answer": "Replication copies data [1].",
        "citations": [Citation(title="Replication", url="http://kb/r")],
        "confidence": 0.9,
        "escalated": False,
        "escalation_url": None,
    }
    base.update(over)
    return Answer(**base)


# ======================================================================= #
# Auth on the admin surface
# ======================================================================= #
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/ingest"),
        ("post", "/admin/reindex"),
        ("get", "/admin/gaps"),
        ("get", "/admin/inform"),
        ("post", "/admin/inform"),
        ("post", "/admin/inform/1/approve"),
        ("post", "/admin/inform/1/reject"),
        ("post", "/admin/inform/1/revoke"),
        ("get", "/metrics"),
        ("post", "/notify"),
    ],
)
def test_every_admin_route_requires_the_api_key(ctx, method, path) -> None:
    call = getattr(ctx["client"], method)
    resp = call(path) if method == "get" else call(path, json={})
    assert resp.status_code == 401


def test_a_non_ascii_authorization_header_is_a_clean_401(ctx) -> None:
    """Comparing str with compare_digest raises TypeError on non-ASCII, which
    would surface as a 500 and distinguish a malformed key from a wrong one."""

    # Sent as raw bytes: httpx will not encode a non-ASCII header value itself.
    resp = ctx["client"].post(
        "/ask", json={"question": "x"}, headers={"Authorization": "Bearer \u00fc".encode()}
    )
    assert resp.status_code == 401


# ======================================================================= #
# Inform loop
# ======================================================================= #
def test_submit_queues_pending_and_indexes_nothing(ctx) -> None:
    resp = ctx["client"].post(
        "/admin/inform",
        json={"question": "How do I rotate keys?", "answer": "Run the script.", "author": "alice"},
        headers=AUTH,
    )

    assert resp.status_code == 200
    assert resp.json() == {"id": 1, "status": "pending"}
    assert ctx["inform"].rows[1]["status"] == "pending"


def test_submit_rejects_a_too_short_question(ctx) -> None:
    resp = ctx["client"].post("/admin/inform", json={"question": "x", "answer": "a"}, headers=AUTH)
    assert resp.status_code == 422


def test_list_defaults_to_the_pending_queue(ctx) -> None:
    ctx["inform"].submit("q1", "a1")
    ctx["inform"].mark(ctx["inform"].submit("q2", "a2"), "approved")

    body = ctx["client"].get("/admin/inform", headers=AUTH).json()

    assert [c["question"] for c in body["candidates"]] == ["q1"]


def test_list_accepts_all_and_rejects_an_unknown_status(ctx) -> None:
    ctx["inform"].submit("q1", "a1")
    assert ctx["client"].get("/admin/inform?status=all", headers=AUTH).status_code == 200
    bad = ctx["client"].get("/admin/inform?status=bogus", headers=AUTH)
    assert bad.status_code == 422


def test_approve_indexes_the_answer_and_records_the_approver(ctx, monkeypatch) -> None:
    indexed = {}

    def fake_index(question, answer, embedder, store, *, candidate_id, **kw):
        indexed.update({"question": question, "candidate_id": candidate_id})
        return 3

    monkeypatch.setattr("kai.pipeline.inform.index_curated_answer", fake_index)
    cid = ctx["inform"].submit("How do I rotate keys?", "Run the script.", author="alice")

    resp = ctx["client"].post(
        f"/admin/inform/{cid}/approve", json={"approver": "bob"}, headers=AUTH
    )

    assert resp.json()["status"] == "approved"
    assert resp.json()["chunks"] == 3
    assert indexed == {"question": "How do I rotate keys?", "candidate_id": cid}
    assert ctx["inform"].rows[cid]["approver"] == "bob"


def test_approve_is_idempotent(ctx, monkeypatch) -> None:
    monkeypatch.setattr("kai.pipeline.inform.index_curated_answer", lambda *a, **k: 2)
    cid = ctx["inform"].submit("q", "a")
    ctx["client"].post(f"/admin/inform/{cid}/approve", json={}, headers=AUTH)

    second = ctx["client"].post(f"/admin/inform/{cid}/approve", json={}, headers=AUTH)

    assert second.status_code == 200
    assert second.json()["status"] == "approved"


def test_approve_404s_on_an_unknown_candidate(ctx) -> None:
    assert ctx["client"].post("/admin/inform/999/approve", json={}, headers=AUTH).status_code == 404


def test_four_eyes_requires_an_approver(monkeypatch) -> None:
    inform = FakeInform()
    monkeypatch.setattr(
        app_module,
        "build_providers",
        lambda *_a, **_k: (object(), object(), FakeStore(), object(), FakeTracker()),
    )
    monkeypatch.setattr("kai.telemetry.Telemetry", lambda *a, **k: FakeTelemetry())
    monkeypatch.setattr("kai.pipeline.inform.InformStore", lambda *a, **k: inform)
    monkeypatch.setattr("kai.pipeline.inform.index_curated_answer", lambda *a, **k: 1)
    client = TestClient(
        app_module.create_app(
            Settings(
                _env_file=None,
                KAI_API_KEY="sekret",
                reranker="noop",
                database_url="",
                inform_require_separate_approver=True,
            )
        ),
        raise_server_exceptions=False,
    )
    cid = inform.submit("q", "a", author="alice")

    missing = client.post(f"/admin/inform/{cid}/approve", json={}, headers=AUTH)
    same = client.post(f"/admin/inform/{cid}/approve", json={"approver": "ALICE"}, headers=AUTH)
    other = client.post(f"/admin/inform/{cid}/approve", json={"approver": "bob"}, headers=AUTH)

    assert missing.status_code == 400
    assert same.status_code == 403, "the approver must differ from the author"
    assert other.status_code == 200


def test_reject_drops_a_pending_candidate_and_is_idempotent(ctx) -> None:
    cid = ctx["inform"].submit("q", "a")

    first = ctx["client"].post(f"/admin/inform/{cid}/reject", headers=AUTH)
    second = ctx["client"].post(f"/admin/inform/{cid}/reject", headers=AUTH)

    assert first.json()["status"] == "rejected"
    assert second.json()["status"] == "rejected"


def test_reject_refuses_an_approved_candidate(ctx) -> None:
    """An approved answer is indexed, so rejecting it would leave it retrievable."""

    cid = ctx["inform"].submit("q", "a")
    ctx["inform"].mark(cid, "approved")

    resp = ctx["client"].post(f"/admin/inform/{cid}/reject", headers=AUTH)

    assert resp.status_code == 409
    assert "revoke" in resp.json()["detail"]


def test_revoke_un_indexes_an_approved_answer(ctx) -> None:
    cid = ctx["inform"].submit("q", "a")
    ctx["inform"].mark(cid, "approved")

    resp = ctx["client"].post(f"/admin/inform/{cid}/revoke", headers=AUTH)

    assert resp.json()["status"] == "revoked"
    assert ctx["store"].deleted == [f"kai-curated:{cid}"]


def test_revoke_refuses_anything_that_is_not_approved(ctx) -> None:
    cid = ctx["inform"].submit("q", "a")
    assert ctx["client"].post(f"/admin/inform/{cid}/revoke", headers=AUTH).status_code == 409


def test_revoke_404s_on_an_unknown_candidate(ctx) -> None:
    assert ctx["client"].post("/admin/inform/999/revoke", headers=AUTH).status_code == 404


# ======================================================================= #
# Feedback-driven quarantine
# ======================================================================= #
def test_enough_downvotes_quarantine_a_curated_answer(ctx) -> None:
    cid = ctx["inform"].submit("q", "a")
    ctx["inform"].mark(cid, "approved")
    ctx["inform"].downvote_hits = [{"id": cid, "downvotes": 3}]

    resp = ctx["client"].post("/feedback", json={"question": "q", "verdict": "down"}, headers=AUTH)

    assert resp.json()["quarantined"] == [cid]
    assert ctx["store"].deleted == [f"kai-curated:{cid}"]
    assert ctx["inform"].rows[cid]["status"] == "quarantined"


def test_a_downvote_below_the_threshold_does_not_quarantine(ctx) -> None:
    cid = ctx["inform"].submit("q", "a")
    ctx["inform"].mark(cid, "approved")
    ctx["inform"].downvote_hits = [{"id": cid, "downvotes": 1}]

    resp = ctx["client"].post("/feedback", json={"question": "q", "verdict": "down"}, headers=AUTH)

    assert "quarantined" not in resp.json()
    assert ctx["store"].deleted == []


def test_a_quarantine_failure_never_breaks_the_feedback_call(ctx, monkeypatch) -> None:
    """The 👎 is already recorded by then; a side-effect error must not 500."""

    def boom(question):
        raise RuntimeError("db down")

    monkeypatch.setattr(ctx["inform"], "downvote_curated", boom)

    resp = ctx["client"].post("/feedback", json={"question": "q", "verdict": "down"}, headers=AUTH)

    assert resp.status_code == 200
    assert resp.json()["status"] == "recorded"


# ======================================================================= #
# Ingest, reindex, gaps, metrics, notify
# ======================================================================= #
def test_ingest_returns_the_chunk_count_and_busts_the_cache(ctx, monkeypatch) -> None:
    monkeypatch.setattr(app_module, "ingest_pipeline", lambda *a, **k: 42)
    client = ctx["client"]
    client.post("/ask", json={"question": "cached one"}, headers=AUTH)

    resp = client.post("/ingest", headers=AUTH)
    client.post("/ask", json={"question": "cached one"}, headers=AUTH)

    assert resp.json() == {"ingested": 42}
    # Two real asks either side of the ingest, so the second was not served warm.
    assert [q for q, _ in ctx["telemetry"].asks].count("<cache>") == 0


def test_a_repeat_question_is_served_from_the_cache(ctx) -> None:
    client = ctx["client"]
    client.post("/ask", json={"question": "same question"}, headers=AUTH)
    client.post("/ask", json={"question": "  SAME   question  "}, headers=AUTH)

    assert [q for q, _ in ctx["telemetry"].asks].count("<cache>") == 1


def test_reindex_reports_what_it_rebuilt(ctx, monkeypatch) -> None:
    monkeypatch.setattr("kai.pipeline.ingest.reindex", lambda *a, **k: {"chunks": 10, "curated": 2})
    resp = ctx["client"].post("/admin/reindex", headers=AUTH)
    assert resp.json() == {"chunks": 10, "curated": 2}


def test_gaps_clamps_the_limit(ctx) -> None:
    ctx["client"].get("/admin/gaps?limit=99999", headers=AUTH)
    assert ctx["telemetry"].gap_limit == 500
    ctx["client"].get("/admin/gaps?limit=0", headers=AUTH)
    assert ctx["telemetry"].gap_limit == 1


def test_metrics_is_prometheus_text(ctx) -> None:
    resp = ctx["client"].get("/metrics", headers=AUTH)
    assert resp.status_code == 200
    assert "kai_asks_total" in resp.text


def test_notify_needs_a_configured_webex_token(ctx) -> None:
    resp = ctx["client"].post("/notify", json={"email": "a@b.co", "message": "hi"}, headers=AUTH)
    assert resp.status_code == 400
    assert "WEBEX_BOT_TOKEN" in resp.json()["detail"]


@pytest.mark.parametrize("email", ["not-an-email", "a@b", "a b@c.co", "@b.co", "a@.co"])
def test_notify_rejects_a_malformed_address(monkeypatch, email) -> None:
    monkeypatch.setattr(
        app_module,
        "build_providers",
        lambda *_a, **_k: (object(), object(), FakeStore(), object(), FakeTracker()),
    )
    monkeypatch.setattr("kai.telemetry.Telemetry", lambda *a, **k: FakeTelemetry())
    monkeypatch.setattr("kai.pipeline.inform.InformStore", lambda *a, **k: FakeInform())
    client = TestClient(
        app_module.create_app(
            Settings(
                _env_file=None,
                KAI_API_KEY="sekret",
                reranker="noop",
                database_url="",
                WEBEX_BOT_TOKEN="tok",
            )
        ),
        raise_server_exceptions=False,
    )

    resp = client.post("/notify", json={"email": email, "message": "hi"}, headers=AUTH)

    assert resp.status_code == 422


def test_notify_reports_a_failed_delivery_instead_of_200(monkeypatch) -> None:
    monkeypatch.setattr(
        app_module,
        "build_providers",
        lambda *_a, **_k: (object(), object(), FakeStore(), object(), FakeTracker()),
    )
    monkeypatch.setattr("kai.telemetry.Telemetry", lambda *a, **k: FakeTelemetry())
    monkeypatch.setattr("kai.pipeline.inform.InformStore", lambda *a, **k: FakeInform())
    monkeypatch.setattr("kai.chat.webex.send_direct_message", lambda *a, **k: False)
    client = TestClient(
        app_module.create_app(
            Settings(
                _env_file=None,
                KAI_API_KEY="sekret",
                reranker="noop",
                database_url="",
                WEBEX_BOT_TOKEN="tok",
            )
        ),
        raise_server_exceptions=False,
    )

    resp = client.post("/notify", json={"email": "a@b.co", "message": "hi"}, headers=AUTH)

    assert resp.status_code == 502


def test_escalate_degrades_when_the_tracker_is_down(ctx, monkeypatch) -> None:
    def boom(title, body):
        raise RuntimeError("jira down")

    monkeypatch.setattr(ctx["tracker"], "create_issue", boom)

    resp = ctx["client"].post("/escalate", json={"question": "q"}, headers=AUTH)

    assert resp.status_code == 200
    assert resp.json() == {"status": "escalated", "escalation_url": None}


def test_docs_are_hidden_once_an_api_key_is_set(ctx) -> None:
    """Keyed mode is the production posture: do not enumerate /admin/* to scanners."""

    assert ctx["client"].get("/openapi.json").status_code == 404
    assert ctx["client"].get("/docs").status_code == 404
    banner = ctx["client"].get("/").json()
    assert banner["docs"] is None
    assert "endpoints" not in banner


def test_docs_stay_open_without_an_api_key(monkeypatch) -> None:
    monkeypatch.setattr(
        app_module,
        "build_providers",
        lambda *_a, **_k: (object(), object(), FakeStore(), object(), FakeTracker()),
    )
    monkeypatch.setattr("kai.telemetry.Telemetry", lambda *a, **k: FakeTelemetry())
    monkeypatch.setattr("kai.pipeline.inform.InformStore", lambda *a, **k: FakeInform())
    client = TestClient(
        app_module.create_app(Settings(_env_file=None, reranker="noop", database_url=""))
    )

    assert client.get("/openapi.json").status_code == 200
    assert client.get("/").json()["endpoints"]["ask"] == "POST /ask"

"""Telemetry and the Inform queue against a real Postgres.

Both are Postgres-backed and lazily create their own schema, so fakes cannot show
whether the DDL, the gap aggregation or the normalised downvote match actually
work. Telemetry writes are also best-effort by design, which is only meaningful
if a broken database really does leave the caller unaffected.

Skipped unless KAI_TEST_DATABASE_URL points at a live database (see conftest).
"""

from __future__ import annotations

import pytest

from kai.pipeline.inform import InformStore
from kai.telemetry import Telemetry

pytestmark = pytest.mark.integration


@pytest.fixture
def clean_db(integration_db_url: str) -> str:
    """Drop the telemetry/inform tables so each test starts from a known state."""

    import psycopg

    with psycopg.connect(integration_db_url) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS kai_questions")
            cur.execute("DROP TABLE IF EXISTS kai_feedback")
            cur.execute("DROP TABLE IF EXISTS kai_kb_candidates")
        conn.commit()
    return integration_db_url


# ======================================================================= #
# Telemetry
# ======================================================================= #
def test_record_ask_creates_its_schema_and_counts(clean_db: str) -> None:
    t = Telemetry(clean_db)
    t.record_ask(
        "q1",
        confidence=0.9,
        escalated=False,
        citation_count=2,
        escalation_url=None,
        duration_ms=120,
    )
    t.record_ask(
        "q2", confidence=0.1, escalated=True, citation_count=0, escalation_url="u", duration_ms=80
    )

    metrics = t.metrics_text()

    assert "kai_asks_total 2" in metrics
    assert "kai_escalations_total 1" in metrics
    assert "kai_ask_duration_ms_sum 200" in metrics


def test_a_cache_hit_counts_as_an_ask_without_skewing_latency(clean_db: str) -> None:
    t = Telemetry(clean_db)
    t.record_ask(
        "q", confidence=0.9, escalated=False, citation_count=1, escalation_url=None, duration_ms=500
    )
    t.record_cache_hit()

    metrics = t.metrics_text()

    assert "kai_asks_total 2" in metrics
    assert "kai_ask_cache_hits_total 1" in metrics
    assert "kai_ask_duration_ms_count 1" in metrics, "a 0ms cache hit must not enter the average"


def test_gaps_ranks_the_most_escalated_questions(clean_db: str) -> None:
    t = Telemetry(clean_db)
    for _ in range(3):
        t.record_ask(
            "How do I rotate keys?",
            confidence=0.2,
            escalated=True,
            citation_count=0,
            escalation_url=None,
            duration_ms=10,
        )
    t.record_ask(
        "Something else?",
        confidence=0.2,
        escalated=True,
        citation_count=0,
        escalation_url=None,
        duration_ms=10,
    )
    t.record_ask(
        "An answered one",
        confidence=0.9,
        escalated=False,
        citation_count=1,
        escalation_url=None,
        duration_ms=10,
    )

    gaps = t.gaps(limit=10)

    assert [g["question"] for g in gaps][:1] == ["how do i rotate keys?"]
    assert gaps[0]["count"] == 3
    assert "an answered one" not in [g["question"] for g in gaps], "only escalations are gaps"


def test_gaps_normalises_whitespace_and_case_when_grouping(clean_db: str) -> None:
    t = Telemetry(clean_db)
    for q in ("Reset   my password", "reset my password", "RESET MY PASSWORD"):
        t.record_ask(
            q, confidence=0.2, escalated=True, citation_count=0, escalation_url=None, duration_ms=10
        )

    gaps = t.gaps(limit=10)

    assert len(gaps) == 1
    assert gaps[0]["count"] == 3


def test_question_text_can_be_withheld_while_the_hash_is_kept(clean_db: str) -> None:
    """The PII-sensitive posture: rows are still written, the text is not."""

    t = Telemetry(clean_db, store_question_text=False)
    t.record_ask(
        "secret question",
        confidence=0.2,
        escalated=True,
        citation_count=0,
        escalation_url=None,
        duration_ms=10,
    )

    assert t.gaps(limit=10) == [], "gaps aggregates on the text, which was not stored"


def test_feedback_is_persisted_and_counted(clean_db: str) -> None:
    t = Telemetry(clean_db)
    t.record_feedback("q", "up", "alice")
    t.record_feedback("q", "down", "bob")

    metrics = t.metrics_text()

    assert "kai_feedback_up_total 1" in metrics
    assert "kai_feedback_down_total 1" in metrics


def test_a_broken_database_never_breaks_the_caller() -> None:
    """Telemetry is best-effort: a write failure is logged, never raised."""

    t = Telemetry("postgresql://nobody@127.0.0.1:1/nope")

    t.record_ask(
        "q", confidence=0.5, escalated=False, citation_count=0, escalation_url=None, duration_ms=1
    )
    t.record_feedback("q", "up", "a")

    assert t.gaps() == []
    assert "kai_asks_total 1" in t.metrics_text(), "in-process counters still work"


# ======================================================================= #
# InformStore
# ======================================================================= #
def test_submit_queues_a_pending_candidate(clean_db: str) -> None:
    store = InformStore(clean_db)

    cid = store.submit("How do I rotate keys?", "Use the rotate script.", author="alice")

    cand = store.get(cid)
    assert cand["status"] == "pending"
    assert cand["question"] == "How do I rotate keys?"
    assert cand["author"] == "alice"
    assert cand["chunks"] == 0


def test_get_returns_none_for_an_unknown_id(clean_db: str) -> None:
    assert InformStore(clean_db).get(999999) is None


def test_list_filters_by_status_and_all_returns_everything(clean_db: str) -> None:
    store = InformStore(clean_db)
    a = store.submit("q1", "a1")
    store.submit("q2", "a2")
    store.mark(a, "approved", chunks=3, approver="bob")

    assert [c["id"] for c in store.list(status="approved")] == [a]
    assert len(store.list(status="pending")) == 1
    assert len(store.list(status="")) == 2, "an empty status means every candidate"


def test_list_pages_with_limit_and_offset(clean_db: str) -> None:
    store = InformStore(clean_db)
    for i in range(5):
        store.submit(f"q{i}", "a")

    first = store.list(status="pending", limit=2, offset=0)
    second = store.list(status="pending", limit=2, offset=2)

    assert len(first) == 2 and len(second) == 2
    assert {c["id"] for c in first}.isdisjoint({c["id"] for c in second})


def test_mark_updates_status_chunks_and_approver(clean_db: str) -> None:
    store = InformStore(clean_db)
    cid = store.submit("q", "a", author="alice")

    store.mark(cid, "approved", chunks=7, approver="bob", downvotes=0)

    cand = store.get(cid)
    assert (cand["status"], cand["chunks"], cand["approver"]) == ("approved", 7, "bob")


def test_downvote_increments_every_matching_approved_candidate(clean_db: str) -> None:
    store = InformStore(clean_db)
    first = store.submit("How do I rotate keys?", "a1")
    second = store.submit("how do i   ROTATE keys?", "a2")
    pending = store.submit("How do I rotate keys?", "a3")
    store.mark(first, "approved")
    store.mark(second, "approved")

    hits = store.downvote_curated("How do I rotate keys?")

    assert {h["id"] for h in hits} == {first, second}, "the match is case/whitespace insensitive"
    assert all(h["downvotes"] == 1 for h in hits)
    assert store.get(pending)["downvotes"] == 0, (
        "a pending candidate is not indexed, so not counted"
    )


def test_downvote_ignores_a_question_with_no_curated_answer(clean_db: str) -> None:
    assert InformStore(clean_db).downvote_curated("never asked") == []


def test_downvote_never_raises_on_a_broken_database() -> None:
    """A 👎 has already been recorded by the time this runs; it must not 500."""

    assert InformStore("postgresql://nobody@127.0.0.1:1/nope").downvote_curated("q") == []


def test_the_schema_backfills_columns_on_a_legacy_table(clean_db: str) -> None:
    """An older deployment's table predates asker/approver/downvotes."""

    import psycopg

    with psycopg.connect(clean_db) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE kai_kb_candidates ("
                " id bigserial PRIMARY KEY,"
                " created_at timestamptz NOT NULL DEFAULT now(),"
                " updated_at timestamptz NOT NULL DEFAULT now(),"
                " question text NOT NULL, answer text NOT NULL,"
                " author text NOT NULL DEFAULT '',"
                " status text NOT NULL DEFAULT 'pending',"
                " chunks integer NOT NULL DEFAULT 0)"
            )
        conn.commit()

    store = InformStore(clean_db)
    cid = store.submit("q", "a", asker="asker@example.com")

    assert store.get(cid)["asker"] == "asker@example.com"

"""Ingest, retrieve and answer against a real Postgres, with fake models.

The models are fakes (deterministic hashed embeddings, a scripted LLM) so these
cases stay fast and offline, but every store operation is real SQL. That is the
seam the unit suite cannot cover: incremental skipping, pruning, the curated
Inform path, and the never-fabricate guards running over genuinely retrieved rows.

Skipped unless KAI_TEST_DATABASE_URL points at a live database (see conftest).
"""

from __future__ import annotations

import hashlib
import math

import pytest

from kai.config import Settings
from kai.interfaces import Doc
from kai.pipeline.ask import answer_question
from kai.pipeline.inform import index_curated_answer
from kai.pipeline.ingest import ingest_from
from kai.providers.vectorstore_pgvector import PgVectorStore

pytestmark = pytest.mark.integration

DIM = 64


class HashEmbedder:
    """Deterministic bag-of-words embedder. Shared words move vectors together."""

    dimensions = DIM

    def embed(self, texts):
        out = []
        for text in texts:
            vec = [0.0] * DIM
            for word in str(text).lower().split():
                digest = hashlib.sha256(word.encode()).digest()
                vec[digest[0] % DIM] += 1.0
            norm = math.sqrt(sum(x * x for x in vec)) or 1.0
            out.append([x / norm for x in vec])
        return out


class ScriptedLLM:
    """Returns the next queued reply; records every prompt it was given."""

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.prompts: list[tuple[str, str]] = []

    def complete(self, system, user, *, max_tokens=1024, temperature=0.1) -> str:
        self.prompts.append((system, user))
        return self.replies.pop(0) if self.replies else "I don't know"


class RecordingTracker:
    def __init__(self) -> None:
        self.issues: list[tuple[str, str]] = []

    def create_issue(self, title: str, body: str) -> str:
        self.issues.append((title, body))
        return "https://tracker.example/KAI-1"


class ListSource:
    """A KBSource over a fixed list of docs, with the seen_ids/errors prune contract."""

    def __init__(self, docs: list[Doc]) -> None:
        self.docs = docs
        self.seen_ids: set[str] = set()
        self.errors = 0

    def iter_pages(self):
        self.seen_ids = set()
        for doc in self.docs:
            self.seen_ids.add(doc.id)
            yield doc


HANDBOOK = Doc(
    id="handbook",
    title="VPN access guide",
    url="https://kb.example/vpn",
    html="Request VPN access through the service desk portal. "
    "Approval takes one business day and the token is emailed to you.",
    space="test",
    content_type="text",
)
ONCALL = Doc(
    id="oncall",
    title="On-call runbook",
    url="https://kb.example/oncall",
    html="The on-call engineer acknowledges a page within fifteen minutes "
    "and escalates to the secondary if unacknowledged.",
    space="test",
    content_type="text",
)


def _settings(**over) -> Settings:
    base = {
        "reranker": "noop",
        "multi_query": False,
        "query_rewrite": False,
        "verify_answers": False,
        "sentence_grounding": False,
        "confidence_threshold": 0.3,
        "answer_grounding_min": 0.0,
        "top_k": 5,
        "database_url": "",
    }
    base.update(over)
    return Settings(_env_file=None, **base)


@pytest.fixture
def store(integration_db_url: str, pg_table: str) -> PgVectorStore:
    return PgVectorStore(database_url=integration_db_url, table=pg_table)


def test_ingest_then_answer_cites_the_right_page(store: PgVectorStore) -> None:
    embedder = HashEmbedder()
    ingest_from(embedder, store, ListSource([HANDBOOK, ONCALL]))

    llm = ScriptedLLM("Request VPN access through the service desk portal [1].")
    answer = answer_question(
        "How do I request VPN access?", embedder, llm, store, RecordingTracker(), _settings()
    )

    assert not answer.escalated
    assert [c.url for c in answer.citations] == ["https://kb.example/vpn"]
    # The retrieved page must actually be in the prompt the model saw.
    assert "service desk portal" in llm.prompts[0][1]


def test_an_off_topic_question_escalates_without_calling_the_model(
    store: PgVectorStore,
) -> None:
    embedder = HashEmbedder()
    ingest_from(embedder, store, ListSource([HANDBOOK, ONCALL]))

    llm = ScriptedLLM("this answer must never be generated")
    tracker = RecordingTracker()
    answer = answer_question(
        "What is the airspeed velocity of an unladen swallow?",
        embedder,
        llm,
        store,
        tracker,
        _settings(confidence_threshold=0.9),
    )

    assert answer.escalated
    assert answer.citations == []
    assert llm.prompts == [], "a retrieval-gated escalation must not spend a generation"
    assert len(tracker.issues) == 1


def test_a_second_ingest_skips_unchanged_documents(store: PgVectorStore) -> None:
    embedder = HashEmbedder()
    source = ListSource([HANDBOOK, ONCALL])

    first = ingest_from(embedder, store, source)
    second = ingest_from(embedder, store, source)

    assert first > 0
    assert second == 0, "unchanged documents must cost zero embed calls and zero writes"
    assert sorted(store.list_doc_ids()) == ["handbook", "oncall"]


def test_an_edited_document_is_re_ingested(store: PgVectorStore) -> None:
    embedder = HashEmbedder()
    ingest_from(embedder, store, ListSource([HANDBOOK]))

    edited = Doc(**{**HANDBOOK.__dict__, "html": HANDBOOK.html + " Tokens expire after 90 days."})
    written = ingest_from(embedder, store, ListSource([edited]))

    assert written > 0
    hits = store.search(
        query_vector=embedder.embed(["tokens expire"])[0], query_text="expire", top_k=5
    )
    assert any("90 days" in h.chunk.text for h in hits)


def test_a_shrunk_document_leaves_no_orphaned_chunks(store: PgVectorStore) -> None:
    embedder = HashEmbedder()
    long_doc = Doc(
        **{**HANDBOOK.__dict__, "html": " ".join(f"section {i} body text" for i in range(400))}
    )
    ingest_from(embedder, store, ListSource([long_doc]))
    before = len(
        store.search(
            query_vector=embedder.embed(["section body"])[0], query_text="section", top_k=50
        )
    )

    short = Doc(**{**HANDBOOK.__dict__, "html": "section 0 body text"})
    ingest_from(embedder, store, ListSource([short]))
    after = store.search(
        query_vector=embedder.embed(["section body"])[0], query_text="section", top_k=50
    )

    assert before > 1
    assert len(after) == 1, "the higher ordinals from the longer version must be deleted"


def test_prune_deletes_a_document_that_vanished_upstream(store: PgVectorStore) -> None:
    embedder = HashEmbedder()
    ingest_from(embedder, store, ListSource([HANDBOOK, ONCALL]))

    ingest_from(embedder, store, ListSource([HANDBOOK]), prune=True)

    assert store.list_doc_ids() == ["handbook"]


def test_prune_refuses_a_crawl_that_came_back_empty(store: PgVectorStore) -> None:
    """The mass-delete guard: an outage that yields no documents must not wipe the corpus."""

    embedder = HashEmbedder()
    ingest_from(embedder, store, ListSource([HANDBOOK, ONCALL]))

    ingest_from(embedder, store, ListSource([]), prune=True)

    assert sorted(store.list_doc_ids()) == ["handbook", "oncall"]


def test_prune_keeps_curated_answers(store: PgVectorStore) -> None:
    """Curated entries have no crawl source, so a crawl never sees them."""

    embedder = HashEmbedder()
    ingest_from(embedder, store, ListSource([HANDBOOK]))
    index_curated_answer(
        "What is the laptop refresh cycle?",
        "Laptops are refreshed every three years.",
        embedder,
        store,
        candidate_id=7,
    )

    ingest_from(embedder, store, ListSource([HANDBOOK]), prune=True)

    assert "kai-curated:7" in store.list_doc_ids()


def test_a_curated_answer_becomes_retrievable_and_is_labelled(store: PgVectorStore) -> None:
    embedder = HashEmbedder()
    ingest_from(embedder, store, ListSource([HANDBOOK]))
    index_curated_answer(
        "What is the laptop refresh cycle?",
        "Laptops are refreshed every three years.",
        embedder,
        store,
        candidate_id=7,
    )

    llm = ScriptedLLM("Laptops are refreshed every three years [1].")
    answer = answer_question(
        "What is the laptop refresh cycle?",
        embedder,
        llm,
        store,
        RecordingTracker(),
        _settings(),
    )

    assert not answer.escalated
    assert "community-curated" in answer.answer


def test_a_fabricated_number_escalates_over_real_retrieved_rows(store: PgVectorStore) -> None:
    embedder = HashEmbedder()
    ingest_from(embedder, store, ListSource([HANDBOOK]))

    llm = ScriptedLLM("VPN approval takes 1,234,567 business days [1].")
    tracker = RecordingTracker()
    answer = answer_question(
        "How long does VPN approval take?", embedder, llm, store, tracker, _settings()
    )

    assert answer.escalated, "a number absent from every source must not be shipped"
    assert len(tracker.issues) == 1


def test_a_control_character_in_a_source_body_does_not_break_ingest(
    store: PgVectorStore,
) -> None:
    """Postgres rejects NUL in a text column, so the chunker has to strip it."""

    embedder = HashEmbedder()
    dirty = Doc(**{**HANDBOOK.__dict__, "html": "Request VPN\x00 access through\x07 the portal."})

    written = ingest_from(embedder, store, ListSource([dirty]))

    assert written > 0
    hits = store.search(
        query_vector=embedder.embed(["vpn access"])[0], query_text="portal", top_k=5
    )
    assert hits and "\x00" not in hits[0].chunk.text

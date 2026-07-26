"""PgVectorStore against a real Postgres + pgvector.

The unit suite covers this store with fakes, which proves the orchestration but
not the SQL. These cases exercise the statements themselves: DDL, upsert/replace
transactions, the hybrid vector + full-text RRF query, filters, pruning support
and the vector-type guard.

Skipped unless KAI_TEST_DATABASE_URL points at a live database (see conftest).
"""

from __future__ import annotations

import math

import pytest

from kai.interfaces import Chunk
from kai.providers.vectorstore_pgvector import PgVectorStore

pytestmark = pytest.mark.integration

DIM = 8


def _vec(*weights: float) -> list[float]:
    """A unit-length DIM-wide vector from the leading weights (rest zero)."""

    v = list(weights) + [0.0] * (DIM - len(weights))
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norm for x in v]


def _chunk(doc_id: str, ordinal: int, text: str, *, title: str = "", url: str = "") -> Chunk:
    return Chunk(
        id=Chunk.make_id(doc_id, ordinal),
        doc_id=doc_id,
        title=title or doc_id,
        url=url,
        text=text,
        space="test",
        ordinal=ordinal,
    )


@pytest.fixture
def store(integration_db_url: str, pg_table: str) -> PgVectorStore:
    s = PgVectorStore(database_url=integration_db_url, table=pg_table)
    s.ensure_schema(DIM)
    return s


def test_ensure_schema_is_idempotent(store: PgVectorStore) -> None:
    store.ensure_schema(DIM)
    store.ensure_schema(DIM)
    assert store.current_dimensions() == DIM


def test_upsert_then_search_returns_the_stored_chunk(store: PgVectorStore) -> None:
    chunks = [_chunk("doc-a", 0, "Replication copies data between brokers.")]
    store.upsert(chunks, [_vec(1, 0)])

    hits = store.search(query_vector=_vec(1, 0), query_text="replication", top_k=5)

    assert [h.chunk.id for h in hits] == ["doc-a#0"]
    assert hits[0].chunk.text.startswith("Replication copies data")
    # vector_score is cosine similarity against an identical vector.
    assert hits[0].vector_score == pytest.approx(1.0, abs=1e-6)


def test_upsert_is_idempotent_on_chunk_id(store: PgVectorStore) -> None:
    store.upsert([_chunk("doc-a", 0, "first text")], [_vec(1, 0)])
    store.upsert([_chunk("doc-a", 0, "second text")], [_vec(1, 0)])

    hits = store.search(query_vector=_vec(1, 0), query_text="text", top_k=10)

    assert len(hits) == 1, "re-upserting the same chunk id must replace, not duplicate"
    assert hits[0].chunk.text == "second text"


def test_hybrid_search_finds_a_lexical_match_an_orthogonal_vector_would_miss(
    store: PgVectorStore,
) -> None:
    """The lexical arm has to contribute: an exact keyword hit must surface even
    when its vector points away from the query."""

    store.upsert(
        [
            _chunk("vec-doc", 0, "unrelated filler prose about nothing in particular"),
            _chunk("lex-doc", 0, "the quorum controller elects a leader"),
        ],
        [_vec(1, 0), _vec(0, 1)],
    )

    hits = store.search(query_vector=_vec(1, 0), query_text="quorum controller", top_k=10)

    assert "lex-doc#0" in {h.chunk.id for h in hits}


def test_search_or_fallback_survives_a_word_absent_from_the_corpus(
    store: PgVectorStore,
) -> None:
    """websearch_to_tsquery ANDs every term, so one unknown word would zero the
    lexical arm. The OR fallback keeps the known terms working."""

    store.upsert([_chunk("doc-a", 0, "the quorum controller elects a leader")], [_vec(0, 1)])

    hits = store.search(
        query_vector=_vec(1, 0),  # orthogonal, so only the lexical arm can match
        query_text="quorum zzzznotacorpusword",
        top_k=10,
    )

    assert [h.chunk.id for h in hits] == ["doc-a#0"]


def test_search_respects_a_doc_id_filter(store: PgVectorStore) -> None:
    store.upsert(
        [_chunk("keep", 0, "shared vocabulary here"), _chunk("drop", 0, "shared vocabulary here")],
        [_vec(1, 0), _vec(1, 0)],
    )

    hits = store.search(
        query_vector=_vec(1, 0), query_text="shared", top_k=10, filters={"doc_id": "keep"}
    )

    assert {h.chunk.doc_id for h in hits} == {"keep"}


def test_search_rejects_an_unknown_filter_column(store: PgVectorStore) -> None:
    with pytest.raises(ValueError, match="Unsupported search filter"):
        store.search(query_vector=_vec(1, 0), query_text="x", top_k=1, filters={"secret": "1"})


def test_search_rejects_a_query_vector_of_the_wrong_width(store: PgVectorStore) -> None:
    store.upsert([_chunk("doc-a", 0, "text")], [_vec(1, 0)])
    with pytest.raises(ValueError, match="does not match store dimensions"):
        store.search(query_vector=[0.1, 0.2], query_text="text", top_k=1)


def test_upsert_rejects_a_vector_of_the_wrong_width(store: PgVectorStore) -> None:
    with pytest.raises(ValueError, match="width"):
        store.upsert([_chunk("doc-a", 0, "text")], [[0.1, 0.2]])


def test_replace_swaps_a_documents_rows_and_records_its_hash(store: PgVectorStore) -> None:
    store.upsert(
        [_chunk("doc-a", 0, "old one"), _chunk("doc-a", 1, "old two")],
        [_vec(1, 0), _vec(1, 0)],
    )

    store.replace("doc-a", [_chunk("doc-a", 0, "new only")], [_vec(1, 0)], content_hash="h1")

    hits = store.search(query_vector=_vec(1, 0), query_text="only", top_k=10)
    assert [h.chunk.id for h in hits] == ["doc-a#0"], "the orphaned ordinal must be gone"
    assert store.doc_hashes() == {"doc-a": "h1"}


def test_replace_with_no_chunks_deletes_and_records_no_hash(store: PgVectorStore) -> None:
    """An empty replacement removed the doc, so writing a hash would mark a
    non-existent document as up to date and the next ingest would skip it."""

    store.upsert([_chunk("doc-a", 0, "text")], [_vec(1, 0)])
    store.set_doc_hash("doc-a", "stale")

    store.replace("doc-a", [], [], content_hash="fresh")

    assert store.list_doc_ids() == []
    assert store.doc_hashes().get("doc-a") == "stale", "an empty replace must not stamp a new hash"


def test_delete_removes_rows_and_the_content_hash(store: PgVectorStore) -> None:
    store.upsert([_chunk("doc-a", 0, "text")], [_vec(1, 0)])
    store.set_doc_hash("doc-a", "h1")

    store.delete("doc-a")

    assert store.list_doc_ids() == []
    assert store.doc_hashes() == {}


def test_doc_hashes_is_empty_before_the_side_table_exists(store: PgVectorStore) -> None:
    assert store.doc_hashes() == {}


def test_clear_doc_hashes_keeps_the_live_rows(store: PgVectorStore) -> None:
    """This is what makes reindex safe: hashes go, vectors stay retrievable."""

    store.upsert([_chunk("doc-a", 0, "text")], [_vec(1, 0)])
    store.set_doc_hash("doc-a", "h1")

    store.clear_doc_hashes()

    assert store.doc_hashes() == {}
    assert store.list_doc_ids() == ["doc-a"]


def test_a_vector_type_change_on_an_existing_table_is_refused(
    integration_db_url: str, pg_table: str
) -> None:
    """Changing VECTOR_TYPE in place would leave the column at its old type and
    every later query would fail on an opaque operator error."""

    PgVectorStore(database_url=integration_db_url, table=pg_table).ensure_schema(DIM)

    halfvec = PgVectorStore(database_url=integration_db_url, table=pg_table, vector_type="halfvec")
    with pytest.raises(ValueError, match="re-ingest"):
        halfvec.ensure_schema(DIM)


def test_halfvec_round_trips_on_its_own_table(integration_db_url: str, pg_table: str) -> None:
    store = PgVectorStore(database_url=integration_db_url, table=pg_table, vector_type="halfvec")
    store.ensure_schema(DIM)
    store.upsert([_chunk("doc-a", 0, "half precision text")], [_vec(1, 0)])

    hits = store.search(query_vector=_vec(1, 0), query_text="precision", top_k=5)

    assert [h.chunk.id for h in hits] == ["doc-a#0"]
    assert hits[0].vector_score == pytest.approx(1.0, abs=1e-2)


def test_search_returns_nothing_for_a_non_positive_top_k(store: PgVectorStore) -> None:
    store.upsert([_chunk("doc-a", 0, "text")], [_vec(1, 0)])
    assert store.search(query_vector=_vec(1, 0), query_text="text", top_k=0) == []


def test_text_containing_a_nul_byte_is_rejected_by_postgres(store: PgVectorStore) -> None:
    """Documents the constraint the chunker's control-character strip exists for:
    Postgres text columns cannot hold NUL, so unsanitised bodies fail here."""

    with pytest.raises(Exception, match=r"(?i)nul"):
        store.upsert([_chunk("doc-a", 0, "before\x00after")], [_vec(1, 0)])

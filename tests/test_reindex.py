"""reindex: a DATA-SAFE vector-index rebuild that PRESERVES curated/inform/telemetry.

The correctness points this pins down:
  * hashes are cleared so every doc re-embeds, but the live rows are NOT dropped —
    a failed/empty crawl can never wipe the corpus (per-doc embed-before-delete +
    prune guards), unlike the old drop-then-reingest;
  * approved curated answers are re-indexed from the Inform queue;
  * an embedding-DIMENSION change is refused (would lose rows in place);
  * a store that can't reindex refuses loudly instead of silently doing nothing.
"""

from __future__ import annotations

import pytest

from kai.interfaces import Doc
from kai.pipeline.ingest import reindex


class FakeEmbedder:
    dimensions = 3

    def embed(self, texts):
        return [[float(len(t) % 7), 1.0, 0.05] for t in texts]


class FakeStore:
    """In-memory stand-in that records the clear-hashes → ensure_schema → upsert flow.

    ``chunks`` (doc_id -> count) models the live rows; clearing hashes must NOT touch
    them (that's the data-safety guarantee). ``dim`` is the existing table's width.
    """

    def __init__(self, preexisting_hashes=None, chunks=None, dim=3):
        self.events: list[str] = []
        self.hashes: dict[str, str] = dict(preexisting_hashes or {})
        self.chunks: dict[str, int] = dict(chunks or {})
        self.dim = dim

    def current_dimensions(self):
        return self.dim

    def clear_doc_hashes(self):
        self.events.append("clear_hashes")
        self.hashes.clear()  # only the hash side-table — live rows stay

    def ensure_schema(self, dimensions):
        self.events.append(f"ensure:{dimensions}")

    def doc_hashes(self):
        return dict(self.hashes)

    def set_doc_hash(self, doc_id, content_hash):
        self.hashes[doc_id] = content_hash

    def delete(self, doc_id):
        self.chunks.pop(doc_id, None)

    def upsert(self, chunks, vectors):
        assert len(chunks) == len(vectors)
        for c in chunks:
            self.chunks[c.doc_id] = self.chunks.get(c.doc_id, 0) + 1

    def list_doc_ids(self):
        return list(self.chunks)


class FakeKB:
    errors = 0

    def __init__(self, docs):
        self._docs = docs

    def iter_pages(self):
        yield from self._docs


class FakeInform:
    def __init__(self, approved):
        self._approved = approved

    def list(self, status="pending", limit=100, offset=0):
        if status != "approved":
            return []
        return list(self._approved)[offset : offset + limit]


def _docs():
    return [
        Doc(
            id="d1",
            title="Kafka",
            url="u1",
            html="Kafka replication copies data " * 5,
            space="files",
            content_type="text",
        ),
        Doc(
            id="d2",
            title="Topics",
            url="u2",
            html="A topic is a partitioned log " * 5,
            space="files",
            content_type="text",
        ),
    ]


def _providers(store, docs):
    # (embedder, llm, store, kb, tracker)
    return (FakeEmbedder(), object(), store, FakeKB(docs), object())


def test_reindex_reembeds_all_despite_stale_hashes():
    # A store that *thinks* every doc is unchanged (stale hashes) must still re-embed
    # all of them, because reindex clears the hashes first.
    store = FakeStore(preexisting_hashes={"d1": "stale", "d2": "stale"}, chunks={"d1": 1, "d2": 1})
    result = reindex(_providers(store, _docs()), target_tokens=200, overlap_tokens=20)

    assert "clear_hashes" in store.events
    assert result["chunks"] > 0
    assert set(store.chunks) == {"d1", "d2"}  # both docs re-indexed despite stale hashes


def test_reindex_empty_crawl_preserves_corpus():
    # THE data-loss guard: if the crawl returns nothing (expired token, upstream 5xx),
    # reindex must NOT wipe the existing rows — the prune mass-delete guard refuses.
    store = FakeStore(chunks={"d1": 3, "d2": 2})
    result = reindex(_providers(store, []))  # empty KB
    assert set(store.chunks) == {"d1", "d2"}  # corpus intact
    assert result["chunks"] == 0


def test_reindex_prune_false_keeps_unseen_docs():
    # A capped/subtree crawl reindexes with prune=False — it must NOT delete docs it
    # never saw (they exist beyond the cap), unlike a full crawl.
    store = FakeStore(chunks={"d1": 1, "d2": 1, "d_unseen": 1})
    reindex(_providers(store, _docs()), prune=False)  # _docs() yields only d1, d2
    assert "d_unseen" in store.chunks  # not pruned


def test_reindex_refuses_dimension_change():
    # A model/dimension change can't be applied in place without losing rows → refuse.
    store = FakeStore(chunks={"d1": 1}, dim=768)
    with pytest.raises(RuntimeError, match="dimension"):
        reindex(_providers(store, _docs()))
    assert store.chunks == {"d1": 1}  # nothing touched


def test_reindex_reindexes_approved_curated_only():
    store = FakeStore()
    inform = FakeInform(
        approved=[
            {"id": 7, "question": "How do I reset my password?", "answer": "Use the portal."},
            {"id": 9, "question": "What is the VPN host?", "answer": "vpn.corp.example."},
        ]
    )
    result = reindex(_providers(store, _docs()), inform_store=inform)

    assert result["curated"] == 2
    # curated docs land under the kai-curated namespace, alongside the source docs
    assert any(k.startswith("kai-curated:") for k in store.chunks)
    assert {"d1", "d2"} <= set(store.chunks)


def test_reindex_without_inform_store_indexes_zero_curated():
    store = FakeStore()
    result = reindex(_providers(store, _docs()))
    assert result["curated"] == 0


class _StoreWithoutReindex:
    """A store that supports ingest but NOT reindex (no clear_doc_hashes)."""

    def ensure_schema(self, dimensions):
        pass

    def doc_hashes(self):
        return {}

    def delete(self, doc_id):
        pass

    def upsert(self, chunks, vectors):
        pass


def test_reindex_refuses_when_store_cannot_reindex():
    providers = (FakeEmbedder(), object(), _StoreWithoutReindex(), FakeKB(_docs()), object())
    with pytest.raises(RuntimeError, match="reindex"):
        reindex(providers)

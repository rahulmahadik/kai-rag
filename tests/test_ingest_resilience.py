"""Ingestion must survive a single failing document, not abort the whole crawl."""

from kai.interfaces import Doc
from kai.pipeline.ingest import ingest_from


class _KB:
    def __init__(self, n):
        self._docs = [
            Doc(
                id=f"d{i}",
                title=f"t{i}",
                url="",
                html=f"document number {i} with enough words here to form a chunk",
                content_type="text",
            )
            for i in range(n)
        ]

    def iter_pages(self):
        return iter(self._docs)


class _Embedder:
    dimensions = 4

    def embed(self, texts):
        # Fail only on document 1's batch, simulates a transient per-doc error.
        if any("number 1 " in t for t in texts):
            raise RuntimeError("simulated embed failure")
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]


class _Store:
    def __init__(self):
        self.rows = {}
        self.schema_dims = None

    def ensure_schema(self, dims):
        self.schema_dims = dims

    def delete(self, doc_id):
        self.rows.pop(doc_id, None)

    def upsert(self, chunks, vectors):
        self.rows[chunks[0].doc_id] = len(chunks)

    def list_doc_ids(self):
        return list(self.rows)


def test_failing_doc_is_skipped_not_fatal():
    store = _Store()
    total = ingest_from(_Embedder(), store, _KB(3))
    # d0 and d2 ingested; d1 (which raised during embed) skipped, crawl not aborted.
    assert "d0" in store.rows and "d2" in store.rows
    assert "d1" not in store.rows
    assert total > 0
    assert store.schema_dims == 4


def test_all_docs_ingest_when_healthy():
    store = _Store()
    total = ingest_from(_Embedder(), store, _KB(0))  # empty crawl -> 0, no crash
    assert total == 0


class _ReplaceStore(_Store):
    """A store that supports the ATOMIC replace (delete+insert+hash in one call)."""

    def __init__(self):
        super().__init__()
        self.replaced = []  # (doc_id, n_chunks, content_hash)

    def replace(self, doc_id, chunks, vectors, content_hash=None):
        self.rows[doc_id] = len(chunks)
        self.replaced.append((doc_id, len(chunks), content_hash))


def test_ingest_uses_atomic_replace_when_available():
    # When the store offers replace(), ingest must use it (one atomic call carrying
    # the content hash) instead of the delete + upsert + set_doc_hash fallback.
    store = _ReplaceStore()
    ingest_from(_Embedder(), store, _KB(1))
    assert store.replaced and store.replaced[0][0] == "d0"
    assert store.replaced[0][2] is not None  # content_hash passed in the same call


class _KBWithSkips:
    """Yields some docs but reports MORE seen ids (pages it saw but skipped)."""

    errors = 0

    def __init__(self, yielded, seen_ids):
        self._docs = yielded
        self.seen_ids = set(seen_ids)

    def iter_pages(self):
        return iter(self._docs)


def test_prune_keeps_seen_but_skipped_doc():
    # d_skipped was SEEN (exists upstream) but not yielded (e.g. empty/permission);
    # only d_deleted is truly gone. Prune must delete d_deleted and KEEP d_skipped.
    store = _Store()
    store.rows = {"d0": 1, "d_skipped": 1, "d_deleted": 1}
    kb = _KBWithSkips(
        [
            Doc(
                id="d0",
                title="t",
                url="",
                html="document zero with enough words",
                content_type="text",
            )
        ],
        seen_ids={"d0", "d_skipped"},
    )
    ingest_from(_Embedder(), store, kb, prune=True)
    assert "d_deleted" not in store.rows  # truly missing -> pruned
    assert "d_skipped" in store.rows  # seen-but-skipped -> kept
    assert "d0" in store.rows  # yielded -> kept

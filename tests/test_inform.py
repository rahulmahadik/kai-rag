"""Inform loop: curated-answer synthesis + indexing (the approval-gated B1 core)."""

from __future__ import annotations

from kai.pipeline.inform import CURATED_SPACE, index_curated_answer


class FakeEmbedder:
    dimensions = 4

    def embed(self, texts):
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]


class FakeStore:
    def __init__(self):
        self.schema_dims = None
        self.deleted: list[str] = []
        self.upserted = []

    def ensure_schema(self, dims):
        self.schema_dims = dims

    def delete(self, doc_id):
        self.deleted.append(doc_id)

    def upsert(self, chunks, vectors):
        self.upserted = list(chunks)


def test_index_curated_answer_writes_curated_doc():
    store = FakeStore()
    n = index_curated_answer(
        "What is the on-call rotation policy?",
        "On-call rotates weekly, handed off Mondays at 10:00, tracked in PagerDuty.",
        FakeEmbedder(),
        store,
        candidate_id=7,
    )
    assert n > 0
    assert store.schema_dims == 4
    # stable id so re-approval overwrites; delete-before-upsert
    assert store.deleted == ["kai-curated:7"]
    assert all(c.space == CURATED_SPACE for c in store.upserted)
    assert store.upserted[0].doc_id == "kai-curated:7"
    body = " ".join(c.text for c in store.upserted).lower()
    assert "pagerduty" in body and "on-call rotation policy" in body  # question is the title


def test_index_curated_empty_answer_writes_nothing():
    store = FakeStore()
    assert index_curated_answer("Q?", "   ", FakeEmbedder(), store, candidate_id=1) == 0
    assert store.upserted == []

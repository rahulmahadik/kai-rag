"""Integration tests for answer_question: the never-fabricate critical path.

Fake providers, no network/DB: assert the ORCHESTRATION (gate short-circuit,
IDK escalation, fabrication guard, tracker resilience, citation finalization,
sentence grounding) rather than the leaf helpers.
"""

from __future__ import annotations

from kai.config import Settings
from kai.interfaces import Chunk, ScoredChunk
from kai.pipeline.ask import answer_question


def _chunk(i: int, text: str) -> ScoredChunk:
    return ScoredChunk(
        chunk=Chunk(
            id=f"c{i}",
            doc_id="d1",
            title="Replication Guide",
            url="http://kb/replication",
            space="KB",
            ordinal=i,
            text=text,
        ),
        score=1.0,
        vector_score=0.9,
    )


class FakeEmbedder:
    dimensions = 4

    def embed(self, texts):
        # Constant unit-ish vectors: sentence-grounding cosine == 1.0 (supported)
        return [[0.5, 0.5, 0.5, 0.5] for _ in texts]


class LowSimEmbedder(FakeEmbedder):
    """First call (sentences+chunks) returns ORTHOGONAL vectors -> unsupported."""

    def embed(self, texts):
        out = []
        for i, _ in enumerate(texts):
            v = [0.0, 0.0, 0.0, 0.0]
            v[i % 4] = 1.0
            out.append(v)
        return out


class FakeLLM:
    def __init__(self, reply: str):
        self.reply = reply
        self.calls = 0

    def complete(self, system, user, *, max_tokens=1024, temperature=0.1):
        self.calls += 1
        return self.reply


class FakeStore:
    def __init__(self, scored):
        self._scored = scored

    def search(self, query_vector, query_text, top_k, filters=None):
        return self._scored[:top_k]


class FakeTracker:
    def __init__(self, raise_on_create: bool = False):
        self.raise_on_create = raise_on_create
        self.created: list[tuple[str, str]] = []

    def create_issue(self, title: str, body: str) -> str:
        if self.raise_on_create:
            raise RuntimeError("tracker down")
        self.created.append((title, body))
        return "http://jira/KAI-1"


def _settings(**over) -> Settings:
    base = {
        "multi_query": False,
        "query_rewrite": False,
        "reranker": "noop",
        "verify_answers": False,
        "confidence_threshold": 0.45,
        "answer_grounding_min": 0.48,
        "sentence_grounding": False,
        "embed_query_prefix": "",
        "embed_passage_prefix": "",
    }
    base.update(over)
    return Settings(**base)


GOOD_CHUNK = _chunk(
    0, "Replication copies partition data from leaders to followers for durability."
)


def test_low_confidence_escalates_without_calling_llm():
    llm = FakeLLM("should never be generated")
    store = FakeStore([])  # nothing retrieved -> conf 0 -> gate fails
    tracker = FakeTracker()
    a = answer_question(
        "how does replication work", FakeEmbedder(), llm, store, tracker, _settings()
    )
    assert a.escalated and llm.calls == 0  # Q4: no LLM spend on a gated question
    assert tracker.created  # ticket filed


def test_idk_reply_escalates():
    llm = FakeLLM("I don't know.")
    a = answer_question(
        "how does replication work",
        FakeEmbedder(),
        llm,
        FakeStore([GOOD_CHUNK]),
        FakeTracker(),
        _settings(),
    )
    assert a.escalated and llm.calls == 1


def test_confident_grounded_answer_with_citation():
    llm = FakeLLM("Replication copies partition data to followers [1].")
    a = answer_question(
        "how does replication work",
        FakeEmbedder(),
        llm,
        FakeStore([GOOD_CHUNK]),
        FakeTracker(),
        _settings(),
    )
    assert not a.escalated
    assert a.citations and a.citations[0].url == "http://kb/replication"
    assert "[1]" in a.answer


def test_fabricated_specifics_escalate():
    llm = FakeLLM("Set org.apache.fabricated.SecretClass in your config to enable replication [1].")
    a = answer_question(
        "how does replication work",
        FakeEmbedder(),
        llm,
        FakeStore([GOOD_CHUNK]),
        FakeTracker(),
        _settings(),
    )
    assert a.escalated  # the dotted specific appears nowhere in the sources


def test_tracker_outage_degrades_not_500():
    llm = FakeLLM("I don't know.")
    a = answer_question(
        "how does replication work",
        FakeEmbedder(),
        llm,
        FakeStore([GOOD_CHUNK]),
        FakeTracker(raise_on_create=True),
        _settings(),
    )
    assert a.escalated and a.escalation_url is None  # degraded, did not raise


def test_escalation_carries_suggested_sources():
    a = answer_question(
        "how does replication work",
        FakeEmbedder(),
        FakeLLM("I don't know."),
        FakeStore([GOOD_CHUNK]),
        FakeTracker(),
        _settings(),
    )
    assert a.escalated
    assert a.suggested_sources and a.suggested_sources[0].url == "http://kb/replication"


def test_escalation_body_excludes_model_draft_by_default():
    tracker = FakeTracker()
    answer_question(
        "how does replication work",
        FakeEmbedder(),
        FakeLLM("I don't know."),
        FakeStore([GOOD_CHUNK]),
        tracker,
        _settings(),
    )
    _title, body = tracker.created[0]
    assert "Model draft" not in body  # M11: unverified text stays local by default


def test_sentence_grounding_escalates_unsupported_answer():
    long_unsupported = (
        "Quantum flux capacitors regulate the primary chronosphere alignment "
        "matrix across seventeen dimensional planes daily [1]."
    )
    llm = FakeLLM(long_unsupported)
    a = answer_question(
        "how does replication work",
        LowSimEmbedder(),
        llm,
        FakeStore([GOOD_CHUNK]),
        FakeTracker(),
        _settings(sentence_grounding=True, sentence_grounding_min=0.55, answer_grounding_min=0.0),
    )
    assert a.escalated


def test_sentence_grounding_off_does_not_gate():
    llm = FakeLLM("Replication copies partition data to followers [1].")
    a = answer_question(
        "how does replication work",
        FakeEmbedder(),
        llm,
        FakeStore([GOOD_CHUNK]),
        FakeTracker(),
        _settings(sentence_grounding=False),
    )
    assert not a.escalated

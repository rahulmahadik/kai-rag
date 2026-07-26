"""The small LLM-driven helpers: rewrite, multi-query expansion, verification.

Each one wraps a model call that can fail, return junk, or return something
harmful, and each is supposed to degrade to a safe default rather than propagate.
These cases drive a fake LLM through those paths.
"""

from __future__ import annotations

import pytest

from kai.interfaces import Chunk, ScoredChunk
from kai.pipeline.multiquery import expand_query
from kai.pipeline.rewrite import rewrite_query
from kai.pipeline.verify import _verdict_passes, verify_answer


class FakeLLM:
    def __init__(self, reply: str = "", raises: Exception | None = None) -> None:
        self.reply = reply
        self.raises = raises
        self.calls: list[tuple[str, str]] = []

    def complete(self, system, user, *, max_tokens=1024, temperature=0.1) -> str:
        self.calls.append((system, user))
        if self.raises is not None:
            raise self.raises
        return self.reply


def _scored(text: str = "source text", title: str = "Page") -> list[ScoredChunk]:
    return [
        ScoredChunk(
            chunk=Chunk(id="d#0", doc_id="d", title=title, url="http://kb/d", text=text),
            score=1.0,
            vector_score=0.9,
        )
    ]


# ======================================================================= #
# rewrite_query
# ======================================================================= #
def test_rewrite_returns_the_corrected_question() -> None:
    assert rewrite_query(FakeLLM("How do I reset my password?"), "how do i reset my pasword") == (
        "How do I reset my password?"
    )


@pytest.mark.parametrize("question", ["", "   "])
def test_rewrite_short_circuits_on_a_blank_question(question) -> None:
    llm = FakeLLM("something")
    assert rewrite_query(llm, question) == question.strip()
    assert llm.calls == [], "a blank question must not cost a model call"


def test_rewrite_falls_back_to_the_original_when_the_model_errors() -> None:
    original = "how do i reset my pasword"
    assert rewrite_query(FakeLLM(raises=RuntimeError("down")), original) == original


@pytest.mark.parametrize("reply", ["", "   ", "\n\n"])
def test_rewrite_falls_back_on_an_empty_reply(reply) -> None:
    assert rewrite_query(FakeLLM(reply), "original text") == "original text"


def test_rewrite_keeps_only_the_first_line_and_unwraps_quotes() -> None:
    llm = FakeLLM('"How do I reset my password?"\nHere is why I changed it...')
    assert rewrite_query(llm, "how do i reset my pasword") == "How do I reset my password?"


def test_rewrite_discards_a_reply_that_ballooned_into_an_answer() -> None:
    """A rewrite is roughly the same length. A much longer one means the model
    elaborated or answered, which would change what the user asked."""

    original = "what is the retention policy"
    llm = FakeLLM("The retention policy " + "is a long elaboration " * 20)
    assert rewrite_query(llm, original) == original


# ======================================================================= #
# expand_query
# ======================================================================= #
def test_expand_returns_one_variant_per_line() -> None:
    llm = FakeLLM("Dynamic Topic Config\nDynamic Topic Config dynamic configuration")
    assert expand_query(llm, "What problem does Dynamic Topic Config address?") == [
        "Dynamic Topic Config",
        "Dynamic Topic Config dynamic configuration",
    ]


def test_expand_strips_numbering_and_quotes() -> None:
    assert expand_query(FakeLLM('1. "alpha"\n2) beta'), "q") == ["alpha", "beta"]


def test_expand_drops_a_variant_identical_to_the_question() -> None:
    assert expand_query(FakeLLM("Kafka\nkafka\nquorum"), "Kafka", n=3) == ["quorum"]


def test_expand_honours_the_requested_count() -> None:
    assert expand_query(FakeLLM("one\ntwo\nthree\nfour"), "q", n=2) == ["one", "two"]


@pytest.mark.parametrize(("question", "n"), [("", 2), ("q", 0), ("  ", 2), ("q", -1)])
def test_expand_short_circuits_without_calling_the_model(question, n) -> None:
    llm = FakeLLM("one\ntwo")
    assert expand_query(llm, question, n=n) == []
    assert llm.calls == []


def test_expand_returns_nothing_when_the_model_errors() -> None:
    """Retrieval must fall back to the single original query, not fail."""

    assert expand_query(FakeLLM(raises=RuntimeError("down")), "q") == []


# ======================================================================= #
# verify_answer
# ======================================================================= #
def test_verify_passes_on_a_pass_verdict() -> None:
    assert verify_answer(FakeLLM("PASS"), "q", "a", _scored()) is True


def test_verify_fails_on_a_fail_verdict() -> None:
    assert verify_answer(FakeLLM("FAIL"), "q", "a", _scored()) is False


def test_verify_fails_closed_with_no_sources() -> None:
    llm = FakeLLM("PASS")
    assert verify_answer(llm, "q", "a", []) is False
    assert llm.calls == [], "with nothing to check against there is nothing to ask"


def test_verify_fails_open_when_the_model_errors() -> None:
    """Verification is an extra guard on top of the deterministic ones, so a
    verifier outage must not block an already-gated answer."""

    assert verify_answer(FakeLLM(raises=RuntimeError("down")), "q", "a", _scored()) is True


def test_verify_prompt_carries_the_question_answer_and_sources() -> None:
    llm = FakeLLM("PASS")
    verify_answer(llm, "the question", "the answer", _scored(text="the source body"))
    _system, user = llm.calls[0]
    assert "the question" in user
    assert "the answer" in user
    assert "the source body" in user


def test_verify_sends_enough_of_each_source_to_cover_a_whole_chunk() -> None:
    """At the old 800-char cap the verifier only saw a chunk's head and failed
    answers whose supporting sentence sat in the tail."""

    tail = "the load bearing fact lives here"
    body = ("filler " * 250) + tail
    llm = FakeLLM("PASS")
    verify_answer(llm, "q", "a", _scored(text=body))
    assert tail in llm.calls[0][1]


@pytest.mark.parametrize(
    ("verdict", "expected"),
    [
        ("PASS", True),
        ("pass", True),
        ("Verdict: PASS", True),
        ("```FAIL```", False),
        ('"FAIL"', False),
        ("FAIL - the answer invents a number", False),
        ("PASS but also FAIL", False),  # FAIL wins, never silently passed
        ("", True),  # unparseable fails open
        ("¯\\_(ツ)_/¯", True),
        ("maybe?", True),
    ],
)
def test_verdict_parsing_is_robust_to_wrapping_and_noise(verdict, expected) -> None:
    assert _verdict_passes(verdict) is expected

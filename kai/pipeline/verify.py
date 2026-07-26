"""Optional answer verification: a second-pass grounding + subject check.

After an answer is generated, the LLM is asked to confirm it is (a) fully
supported by the cited sources and (b) about the EXACT subject of the question.
If not, the pipeline escalates instead of returning a possibly-wrong answer.

This is the strongest guard for the "never give false information" requirement,
it specifically catches the confusable-document failure (answering about the
wrong-but-similar project/event), which the retrieval gate alone cannot, since
that answer *is* grounded in the (wrong) retrieved passage. Cost: one short extra
LLM call per answered question.

Pure orchestration over the LLM Protocol.
"""

from __future__ import annotations

import logging
import re

from kai.interfaces import LLMClient, ScoredChunk

logger = logging.getLogger("kai.verify")

_VERIFY_SYSTEM = (
    "You are a fact-checker for an enterprise knowledge assistant. You are given a "
    "user QUESTION, a proposed ANSWER, and the SOURCE passages it is based on. "
    "Decide whether the answer should be sent.\n"
    "Reply with ONLY one word: PASS or FAIL.\n"
    "Reply FAIL ONLY when there is a CLEAR problem:\n"
    "- the answer states OR COMPUTES a specific fact (a name, number, quantity, "
    "date, title, or email) that does NOT appear verbatim in the sources, "
    "including any arithmetic, total, or count the sources do not state outright; OR\n"
    "- the question is about a specific NAMED item (a particular project, "
    "proposal, event, or person) and the sources are clearly about a DIFFERENT "
    "named item.\n"
    "Otherwise reply PASS. A grounded answer is fine even if it is general, brief, "
    "or only partially complete, including step-by-step instructions, as long as "
    "those steps are drawn from the sources. When uncertain, reply PASS."
)

# Cap each source in the verifier prompt. Sized to hold a whole ~500-token chunk:
# at 800 the verifier saw only a chunk's head and FAILed answers whose supporting
# sentence sat in the tail.
_SRC_CHARS = 2400


def verify_answer(
    llm: LLMClient,
    question: str,
    answer: str,
    scored: list[ScoredChunk],
) -> bool:
    """Return True if the answer passes the grounding + subject check.

    Fails OPEN (returns True) if the verifier call errors. It is an *additional*
    guard on top of the confidence gate and grounding prompt, not a replacement,
    so a verifier hiccup must not block an already-gated answer.
    """

    if not scored:
        return False
    sources = "\n\n".join(
        f"[{i}] {sc.chunk.title}\n{sc.chunk.text[:_SRC_CHARS]}"
        for i, sc in enumerate(scored, start=1)
    )
    user = (
        f"QUESTION: {question}\n\n"
        f"ANSWER: {answer}\n\n"
        f"SOURCES:\n{sources}\n\n"
        "Verdict (reply with the single word PASS or FAIL, nothing else):"
    )
    try:
        verdict = llm.complete(_VERIFY_SYSTEM, user, max_tokens=12, temperature=0.0)
    except Exception as exc:  # noqa: BLE001 - never block answering on a verifier error
        logger.warning(
            "kai_verify_fail_open reason=llm_error err=%s, answer shipped on "
            "deterministic guards only",
            type(exc).__name__,
        )
        return True
    return _verdict_passes(verdict)


def _verdict_passes(verdict: str) -> bool:
    """Interpret the verifier's raw text robustly as PASS (True) / FAIL (False).

    A plain ``startswith("FAIL")`` misses a verdict wrapped in markdown/quotes/a
    prefix (```FAIL, "FAIL", "Verdict: FAIL") and would let a bad answer through.
    We instead look at the alphabetic tokens only: FAIL wins if present (so a
    clearly-failing verdict is never silently passed), else PASS if present, else
    we fail OPEN (treat unparseable text as PASS, verification is an *extra* guard
    on top of the confidence gate, not a replacement, so noise must not block a
    legitimately-gated answer).
    """

    tokens = re.findall(r"[a-z]+", (verdict or "").lower())
    if "fail" in tokens:
        return False
    if "pass" in tokens:
        return True
    logger.warning("kai_verify_fail_open reason=unparseable verdict=%r", verdict)
    return True

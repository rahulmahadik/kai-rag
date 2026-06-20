"""Multi-query expansion: generate alternative phrasings of a question.

Messy or terse questions (typos, casual wording) often miss the document's own
vocabulary on the lexical retrieval arm and embed slightly off on the dense arm.
Asking the LLM for a couple of reformulations — typos fixed, rephrased toward
likely document terms — and retrieving for each widens the candidate pool; the
cross-encoder then reranks the union against the ORIGINAL question, so a better
phrasing can surface the right page without changing what the user asked.

Guardrailed: any failure returns no variants (retrieval falls back to the single
original query). Pure orchestration over the LLM Protocol.
"""

from __future__ import annotations

from kai.interfaces import LLMClient

_EXPAND_SYSTEM = (
    "You turn a user's question into short KEYWORD search queries for a knowledge "
    "base. Output TWO queries containing ONLY the essential topic terms — the "
    "specific subject and any proper/product/feature names — and DROP every "
    "question word and filler (what, how, is, are, the, a, this, page, section, "
    "about, problem, issue, purpose, proposal, design, address, describe, mean). "
    "Keep the subject's exact name and spelling; do NOT substitute synonyms for it. "
    "The FIRST query must be the core subject by itself; the SECOND may add one key "
    "aspect. Output ONLY the queries, one per line — no numbering, quotes, or extra "
    "text.\n"
    'Example question: "What problem does the Dynamic Topic Config proposal address?"\n'
    "Example output:\nDynamic Topic Config\nDynamic Topic Config dynamic configuration"
)


def expand_query(llm: LLMClient, question: str, n: int = 2) -> list[str]:
    """Return up to ``n`` reformulations of ``question`` (empty list on failure)."""

    q = (question or "").strip()
    if not q or n <= 0:
        return []
    try:
        # temperature=0 → DETERMINISTIC reformulations. Same question always yields
        # the same expansions, so a question's answer/escalate decision is stable
        # run-to-run (no random swings). The expansions strip filler ("the … page
        # about", "what problem does … address") down to the document's own
        # vocabulary, which is what recovers verbose phrasings the bare retrieval
        # mis-ranks. See eval/sweep.py + the tuning notes.
        out = llm.complete(_EXPAND_SYSTEM, q, max_tokens=120, temperature=0.0)
    except Exception:  # noqa: BLE001 — never block retrieval on expansion
        return []

    variants: list[str] = []
    seen = {q.lower()}
    for line in (out or "").splitlines():
        v = line.strip().lstrip("0123456789.-) ").strip().strip('"').strip()
        if v and v.lower() not in seen:
            seen.add(v.lower())
            variants.append(v)
        if len(variants) >= n:
            break
    return variants

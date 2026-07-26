"""Optional query normalisation: fix spelling/grammar WITHOUT changing meaning.

Messy real-user input (typos, missing punctuation) hurts retrieval, both the
embedder and the BM25 lexical match degrade on misspellings. When
``query_rewrite`` is enabled, the LLM rewrites the question into clean text
*before* retrieval, preserving the meaning, names and technical terms.

Guardrailed: any failure, empty output, or a suspiciously long rewrite falls back
to the ORIGINAL question, so normalisation can never block or distort a query.
Pure orchestration over the LLM Protocol.
"""

from __future__ import annotations

from kai.interfaces import LLMClient

_SYSTEM = (
    "You fix ONLY spelling and grammar in a user's question. Output ONLY the "
    "corrected question on a single line, nothing else. Preserve the original "
    "meaning EXACTLY. Keep every name, proper noun, product name, abbreviation "
    "and technical term unchanged. Do NOT answer the question, do NOT add or "
    "remove information, do NOT change the intent. If it is already correct, "
    "return it unchanged."
)


def rewrite_query(llm: LLMClient, question: str) -> str:
    """Return a spelling/grammar-corrected ``question`` (meaning preserved).

    Falls back to the original on any problem.
    """

    original = (question or "").strip()
    if not original:
        return original

    try:
        out = llm.complete(_SYSTEM, original, max_tokens=80, temperature=0.0)
    except Exception:  # noqa: BLE001 - never let a rewrite failure block retrieval
        return original

    out = (out or "").strip()
    if not out:
        return original
    # Keep only the first line; strip quotes/markdown the model may wrap around it.
    out = out.splitlines()[0].strip().strip('"').strip("`").strip()
    # Sanity: a real correction is roughly the same length. A wildly longer output
    # means the model elaborated/answered, discard it and keep the original.
    if not out or len(out) > max(140, len(original) * 2):
        return original
    return out

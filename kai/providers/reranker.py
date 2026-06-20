"""Optional cross-encoder reranker (second-stage retrieval precision).

First-stage retrieval (bi-encoder cosine + BM25) is fast but coarse — the right
page is usually in the top-k, just not always at #1 (recall@k >> recall@1). A
**cross-encoder** scores each ``(query, chunk)`` pair *jointly*, which is far more
precise, and we use it to REORDER the candidates so the most relevant chunk is
promoted to the top — improving the cited page + the answer's grounding.

It does not change which chunks were found (recall is unchanged); it only changes
their order. The cosine ``vector_score`` is preserved on each chunk so the
confidence gate (which uses it) is unaffected.

Heavy deps (``sentence-transformers`` / ``torch``) are imported lazily and the
model is loaded once and cached, so importing this module is cheap.
"""

from __future__ import annotations

import threading
from typing import Sequence

from kai.interfaces import ScoredChunk

# model_name -> loaded CrossEncoder (process-wide cache; load is expensive).
_MODELS: dict[str, object] = {}
# Serialise loading so a background pre-warm and an early request can't both
# build the model (they share the one cached instance instead).
_LOCK = threading.Lock()

# ms-marco-MiniLM (the default cross-encoder) has a 512 wordpiece limit and
# predict() SILENTLY truncates a longer (query, chunk) pair — so for a ~500-token
# chunk it would only ever score the chunk's HEAD and never see a fact in the
# back half. To avoid that we slide a character window over each chunk, score
# every window, and keep the MAX — so the whole chunk gets a fair shot at the top
# rank even though each individual scored span fits the model. Char-based (cheap,
# tokenizer-free); ~1000 chars stays comfortably under 512 wordpieces for prose
# and well under it for token-dense text. Overlap keeps a fact off a boundary.
_RERANK_WINDOW_CHARS = 1000
_RERANK_WINDOW_OVERLAP = 200


def _get_model(model_name: str):
    model = _MODELS.get(model_name)
    if model is not None:
        return model
    with _LOCK:
        model = _MODELS.get(model_name)  # re-check inside the lock
        if model is None:
            from sentence_transformers import CrossEncoder  # lazy: pulls torch

            # Downloads on first use, then cached on disk by huggingface_hub.
            model = CrossEncoder(model_name)
            _MODELS[model_name] = model
        return model


def _windows(text: str) -> list[str]:
    """Slide a character window over ``text`` so each span fits the reranker.

    Returns the whole text as a single window when it is short enough; otherwise
    overlapping windows covering the entire text (so no part of a long chunk is
    invisible to the cross-encoder).
    """

    text = text.strip()
    if len(text) <= _RERANK_WINDOW_CHARS:
        return [text] if text else [""]
    step = _RERANK_WINDOW_CHARS - _RERANK_WINDOW_OVERLAP
    out: list[str] = []
    start = 0
    while start < len(text):
        out.append(text[start : start + _RERANK_WINDOW_CHARS])
        if start + _RERANK_WINDOW_CHARS >= len(text):
            break
        start += step
    return out


def rerank(
    query: str,
    scored: Sequence[ScoredChunk],
    model_name: str,
    top_k: int | None = None,
) -> list[ScoredChunk]:
    """Reorder ``scored`` by cross-encoder relevance to ``query``; keep ``top_k``.

    Each chunk is scored as the MAX over character windows of its text, so a long
    chunk is judged on its best-matching span rather than only its first 512
    wordpieces. The fusion ``score`` is replaced by that cross-encoder score (for
    ordering); ``vector_score`` (cosine similarity) is preserved untouched.
    """

    items = list(scored)
    if len(items) <= 1:
        return items[:top_k] if top_k else items

    model = _get_model(model_name)
    # Build one (query, window) pair per window and remember which item it belongs
    # to, so we can collapse window scores back to a single max per chunk.
    pairs: list[tuple[str, str]] = []
    owner: list[int] = []
    for i, sc in enumerate(items):
        for window in _windows(sc.chunk.text):
            pairs.append((query, window))
            owner.append(i)
    raw = model.predict(pairs)

    best = [float("-inf")] * len(items)
    for j, score in enumerate(raw):
        i = owner[j]
        s = float(score)
        if s > best[i]:
            best[i] = s

    order = sorted(range(len(items)), key=lambda i: best[i], reverse=True)
    out = [
        ScoredChunk(
            chunk=items[i].chunk,
            score=best[i],
            vector_score=items[i].vector_score,
            rerank_score=best[i],
        )
        for i in order
    ]
    return out[:top_k] if top_k else out

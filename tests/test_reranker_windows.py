"""The reranker windows long chunks so the 512-token cross-encoder sees all of it.

`_windows` is a pure helper (no model load); we check it returns the whole text as
one window when short, and overlapping windows that fully cover a long text.
"""

from kai.providers.reranker import (
    _RERANK_WINDOW_CHARS,
    _RERANK_WINDOW_OVERLAP,
    _windows,
)


def test_short_text_is_single_window():
    assert _windows("a short chunk") == ["a short chunk"]


def test_long_text_is_covered_by_overlapping_windows():
    text = "x" * (_RERANK_WINDOW_CHARS * 3)
    wins = _windows(text)
    assert len(wins) >= 3
    # Every window fits the size cap.
    assert all(len(w) <= _RERANK_WINDOW_CHARS for w in wins)
    # Consecutive windows overlap (no fact lost on a boundary).
    step = _RERANK_WINDOW_CHARS - _RERANK_WINDOW_OVERLAP
    assert wins[1].startswith(text[step : step + 10])
    # The tail of the text is present in the final window.
    assert text[-10:] in wins[-1]

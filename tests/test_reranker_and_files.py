"""Reranker windowing and the local-file source's skip/decode decisions.

The reranker is tested with a stub cross-encoder so no torch download is needed:
what matters is the windowing, the max-over-windows collapse, and that the cosine
score the confidence gate reads is preserved.
"""

from __future__ import annotations

import pytest

from kai.interfaces import Chunk, ScoredChunk
from kai.providers import reranker as rr
from kai.providers.file_source import (
    CompositeKBSource,
    FileKBSource,
    _decode_text,
    content_type_for,
    extract_text,
    is_unsupported_upload,
    unreadable_reason,
)


class StubEncoder:
    """Scores a (query, window) pair by how many query words the window contains."""

    def __init__(self) -> None:
        self.pairs: list[tuple[str, str]] = []

    def predict(self, pairs):
        self.pairs = list(pairs)
        return [
            float(sum(w in window.lower() for w in query.lower().split()))
            for query, window in pairs
        ]


@pytest.fixture
def stub_model(monkeypatch) -> StubEncoder:
    model = StubEncoder()
    monkeypatch.setattr(rr, "_get_model", lambda name: model)
    return model


def _sc(cid: str, text: str, vector_score: float = 0.5) -> ScoredChunk:
    return ScoredChunk(
        chunk=Chunk(id=cid, doc_id=cid, title="T", url="u", text=text),
        score=0.1,
        vector_score=vector_score,
    )


# ======================================================================= #
# Windowing
# ======================================================================= #
def test_a_short_text_is_one_window() -> None:
    assert rr._windows("short text") == ["short text"]


def test_an_empty_text_still_yields_one_window() -> None:
    assert rr._windows("   ") == [""]


def test_a_long_text_is_split_into_overlapping_windows_covering_all_of_it() -> None:
    text = "".join(str(i % 10) for i in range(3000))

    windows = rr._windows(text)

    assert len(windows) > 1
    assert all(len(w) <= rr._RERANK_WINDOW_CHARS for w in windows)
    assert "".join(dict.fromkeys(windows))  # non-empty
    assert windows[0][:50] in text and text.endswith(windows[-1])
    # Consecutive windows overlap, so a fact on a boundary is never split away.
    assert windows[0][-rr._RERANK_WINDOW_OVERLAP :] == windows[1][: rr._RERANK_WINDOW_OVERLAP]


# ======================================================================= #
# rerank
# ======================================================================= #
def test_rerank_promotes_the_best_matching_chunk(stub_model: StubEncoder) -> None:
    items = [_sc("a", "nothing relevant here"), _sc("b", "quorum controller leader")]

    out = rr.rerank("quorum controller", items, "stub")

    assert [s.chunk.id for s in out] == ["b", "a"]


def test_rerank_preserves_the_cosine_score_the_gate_reads(stub_model: StubEncoder) -> None:
    items = [_sc("a", "irrelevant", vector_score=0.11), _sc("b", "quorum", vector_score=0.92)]

    out = rr.rerank("quorum", items, "stub")

    assert {s.chunk.id: s.vector_score for s in out} == {"a": 0.11, "b": 0.92}
    assert all(s.rerank_score is not None for s in out)
    assert all(s.score == s.rerank_score for s in out)


def test_rerank_scores_a_long_chunk_on_its_best_window(stub_model: StubEncoder) -> None:
    """ms-marco truncates at 512 wordpieces, so without windowing a fact in the
    tail of a long chunk would be invisible to the cross-encoder."""

    tail_match = _sc("tail", ("filler " * 400) + "quorum controller")
    short_miss = _sc("short", "nothing here")

    out = rr.rerank("quorum controller", [tail_match, short_miss], "stub")

    assert out[0].chunk.id == "tail"
    assert len(stub_model.pairs) > 2, "the long chunk must have been split into windows"


def test_rerank_truncates_to_top_k(stub_model: StubEncoder) -> None:
    items = [_sc(str(i), f"quorum {i}") for i in range(5)]
    assert len(rr.rerank("quorum", items, "stub", top_k=2)) == 2


@pytest.mark.parametrize("items", [[], [_sc("only", "text")]])
def test_rerank_short_circuits_on_zero_or_one_candidate(items) -> None:
    """No model is loaded, so this also proves the early return happens first."""

    assert rr.rerank("q", items, "definitely-not-a-real-model") == items


# ======================================================================= #
# Upload classification
# ======================================================================= #
@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("a.html", "html"),
        ("a.HTM", "html"),
        ("a.md", "markdown"),
        ("a.markdown", "markdown"),
        ("a.rst", "markdown"),
        ("a.txt", "text"),
        ("a.pdf", "text"),
        ("noextension", "text"),
        ("", "text"),
    ],
)
def test_content_type_routing(filename, expected) -> None:
    assert content_type_for(filename) == expected


@pytest.mark.parametrize("filename", ["a.docx", "a.PNG", "a.zip", "a.mp4", "a.exe"])
def test_binary_formats_are_flagged_unsupported(filename) -> None:
    assert is_unsupported_upload(filename) is True


@pytest.mark.parametrize("filename", ["a.pdf", "a.txt", "a.md", "a.html", "plain"])
def test_readable_formats_are_not_flagged_unsupported(filename) -> None:
    assert is_unsupported_upload(filename) is False


def test_unreadable_reason_distinguishes_the_three_failure_modes() -> None:
    assert ".docx" in unreadable_reason("report.docx")
    assert "OCR" in unreadable_reason("scan.pdf")
    generic = unreadable_reason("empty.txt")
    assert "OCR" not in generic and "readable text" in generic


# ======================================================================= #
# Decoding
# ======================================================================= #
def test_utf8_and_bom_prefixed_text_decode() -> None:
    assert _decode_text("héllo".encode()) == "héllo"
    assert _decode_text("﻿hello".encode()) == "hello"


def test_utf16_is_detected_by_its_bom() -> None:
    assert _decode_text("hello".encode("utf-16")) == "hello"


def test_empty_bytes_decode_to_an_empty_string() -> None:
    assert _decode_text(b"") == ""


def test_undecodable_binary_is_rejected_rather_than_indexed_as_garbage() -> None:
    """Bytes that are neither UTF-8 nor meaningful cp1252 decode to mostly U+FFFD."""

    assert _decode_text(b"\x81\x8d\x8f\x90\x9d" * 100) is None


def test_control_characters_pass_decoding_and_are_stripped_by_the_chunker() -> None:
    """Control bytes are valid UTF-8, so the decoder keeps them; sanitising happens
    once, in the chunker, which every ingest path goes through."""

    from kai.pipeline.chunk import chunk_body

    raw = b"before\x00\x07after"
    assert "\x00" in _decode_text(raw)
    assert "\x00" not in " ".join(chunk_body(_decode_text(raw)))


def test_cp1252_text_survives_as_a_last_resort() -> None:
    decoded = _decode_text("café".encode("cp1252"))
    assert decoded is not None and "caf" in decoded


def test_extract_text_reads_a_pdf_by_its_magic_bytes_not_its_name(tmp_path) -> None:
    """A .bin that is really a PDF must take the PDF branch."""

    assert extract_text("mislabelled.bin", b"%PDF-1.4 not really a pdf") == ""


def test_extract_text_falls_back_to_decoding_for_a_text_file() -> None:
    assert extract_text("notes.txt", b"plain body") == "plain body"


# ======================================================================= #
# FileKBSource
# ======================================================================= #
def test_file_source_requires_a_real_directory(tmp_path) -> None:
    class S:
        source_dir = ""

    with pytest.raises(ValueError, match="SOURCE_DIR"):
        FileKBSource(S())

    class Missing:
        source_dir = str(tmp_path / "nope")

    with pytest.raises(ValueError, match="not a directory"):
        FileKBSource(Missing())


def _settings_for(tmp_path, max_bytes: int = 0):
    class S:
        source_dir = str(tmp_path)
        file_max_bytes = max_bytes

    return S()


def test_file_source_yields_supported_files_and_skips_the_rest(tmp_path) -> None:
    (tmp_path / "keep.md").write_text("# Heading\n\nbody")
    (tmp_path / "keep.txt").write_text("plain")
    (tmp_path / "skip.docx").write_bytes(b"binary")
    (tmp_path / "empty.txt").write_text("   ")

    docs = list(FileKBSource(_settings_for(tmp_path)).iter_pages())

    assert sorted(d.id for d in docs) == ["keep.md", "keep.txt"]
    assert {d.content_type for d in docs} == {"markdown", "text"}
    assert all(d.url.startswith("file://") for d in docs)


def test_file_source_skips_hidden_and_build_directories(tmp_path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "notes.md").write_text("vcs metadata")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "readme.md").write_text("dependency")
    (tmp_path / ".hidden.md").write_text("dotfile")
    (tmp_path / "real.md").write_text("content")

    docs = list(FileKBSource(_settings_for(tmp_path)).iter_pages())

    assert [d.id for d in docs] == ["real.md"]


def test_a_too_large_file_is_skipped_but_still_counted_as_seen(tmp_path) -> None:
    """Prune must not delete a file that exists upstream and was merely skipped."""

    (tmp_path / "big.txt").write_text("x" * 5000)
    source = FileKBSource(_settings_for(tmp_path, max_bytes=100))

    docs = list(source.iter_pages())

    assert docs == []
    assert source.seen_ids == {"big.txt"}


def test_nested_files_keep_a_posix_relative_id(tmp_path) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "page.md").write_text("body")

    docs = list(FileKBSource(_settings_for(tmp_path)).iter_pages())

    assert [d.id for d in docs] == ["sub/page.md"]


# ======================================================================= #
# CompositeKBSource
# ======================================================================= #
class _Src:
    def __init__(self, docs, boom=False) -> None:
        self.docs = docs
        self.boom = boom
        self.seen_ids = {d.id for d in docs}

    def iter_pages(self):
        yield from self.docs
        if self.boom:
            raise RuntimeError("upstream outage")


def test_composite_chains_sources_and_unions_their_seen_ids(tmp_path) -> None:
    from kai.interfaces import Doc

    a = _Src([Doc(id="a", title="A", url="", html="x")])
    b = _Src([Doc(id="b", title="B", url="", html="y")])

    composite = CompositeKBSource(a, b)
    docs = list(composite.iter_pages())

    assert [d.id for d in docs] == ["a", "b"]
    assert composite.seen_ids == {"a", "b"}
    assert composite.errors == 0


def test_one_failing_source_does_not_stop_the_others(tmp_path) -> None:
    from kai.interfaces import Doc

    failing = _Src([Doc(id="a", title="A", url="", html="x")], boom=True)
    healthy = _Src([Doc(id="b", title="B", url="", html="y")])

    composite = CompositeKBSource(failing, healthy)
    docs = list(composite.iter_pages())

    assert [d.id for d in docs] == ["a", "b"]
    assert composite.errors == 1, "the failure must be visible so prune can refuse"


# ======================================================================= #
# PDF resilience
# ======================================================================= #
def test_one_unreadable_page_does_not_lose_the_whole_pdf(monkeypatch, tmp_path) -> None:
    """A single bad page must cost that page, not the document."""

    class _Page:
        def __init__(self, text, boom=False) -> None:
            self._text, self._boom = text, boom

        def extract_text(self):
            if self._boom:
                raise ValueError("bad page")
            return self._text

    class _Reader:
        def __init__(self, _src) -> None:
            self.pages = [_Page("page one"), _Page("", boom=True), _Page("page three")]

    import pypdf

    monkeypatch.setattr(pypdf, "PdfReader", _Reader)
    (tmp_path / "doc.pdf").write_bytes(b"%PDF-1.4 stub")

    docs = list(FileKBSource(_settings_for(tmp_path)).iter_pages())

    assert len(docs) == 1
    assert "page one" in docs[0].html and "page three" in docs[0].html


def test_a_scanned_pdf_with_no_text_is_skipped_not_ingested_blank(monkeypatch, tmp_path) -> None:
    class _Reader:
        def __init__(self, _src) -> None:
            self.pages = []

    import pypdf

    monkeypatch.setattr(pypdf, "PdfReader", _Reader)
    (tmp_path / "scan.pdf").write_bytes(b"%PDF-1.4 stub")
    source = FileKBSource(_settings_for(tmp_path))

    docs = list(source.iter_pages())

    assert docs == []
    assert source.seen_ids == {"scan.pdf"}, "seen-but-empty must not look deleted to prune"


def test_one_unreadable_file_does_not_abort_the_crawl(monkeypatch, tmp_path) -> None:
    (tmp_path / "a.pdf").write_bytes(b"%PDF-1.4 stub")
    (tmp_path / "b.txt").write_text("readable content")

    import pypdf

    def boom(_src):
        raise RuntimeError("corrupt pdf")

    monkeypatch.setattr(pypdf, "PdfReader", boom)

    docs = list(FileKBSource(_settings_for(tmp_path)).iter_pages())

    assert [d.id for d in docs] == ["b.txt"]


def test_a_utf16_bom_that_is_not_valid_utf16_falls_through(tmp_path) -> None:
    """A truncated UTF-16 export must not be lost just because it carries the BOM."""

    assert _decode_text(b"\xff\xfe" + b"plain ascii after a bogus bom") is not None

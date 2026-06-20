"""Upload-path guards + content-type handling (the things a dropped file hits).

Covers what the two doc-upload audits flagged: a clear "unsupported format" message
instead of a vague "couldn't read", magic-byte PDF detection, HTML/markdown getting
the SAME chunking on upload as on ingest, a crawl that skips junk dirs, and the
Webex download size cap.
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from kai.chat.webex import download_file
from kai.pipeline.chunk import chunk_body
from kai.providers.file_source import (
    FileKBSource,
    content_type_for,
    extract_text,
    is_unsupported_upload,
    unreadable_reason,
)


# ----------------------------------------------------------------------- #
# content_type_for / is_unsupported_upload / unreadable_reason
# ----------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "name,ctype",
    [
        ("page.html", "html"),
        ("page.HTM", "html"),
        ("notes.md", "markdown"),
        ("notes.markdown", "markdown"),
        ("readme.rst", "markdown"),
        ("log.txt", "text"),
        ("scan.pdf", "text"),  # PDF text is plain (no heading markup to honour)
        ("weird.unknown", "text"),
        ("", "text"),
    ],
)
def test_content_type_for(name, ctype):
    assert content_type_for(name) == ctype


@pytest.mark.parametrize(
    "name,unsupported",
    [
        ("report.docx", True),
        ("sheet.XLSX", True),
        ("deck.pptx", True),
        ("photo.png", True),
        ("bundle.zip", True),
        ("notes.txt", False),
        ("doc.pdf", False),
        ("page.html", False),
        ("readme.md", False),
    ],
)
def test_is_unsupported_upload(name, unsupported):
    assert is_unsupported_upload(name) is unsupported


def test_unreadable_reason_is_format_aware():
    docx = unreadable_reason("report.docx")
    assert ".docx" in docx and "PDF" in docx  # names the format + what IS supported
    pdf = unreadable_reason("scan.pdf")
    assert "OCR" in pdf  # a PDF with no text layer → scanned/image, needs OCR
    other = unreadable_reason("empty.txt")
    assert "readable text" in other and "PDF" in other


# ----------------------------------------------------------------------- #
# extract_text — magic-byte PDF detection beats a lying filename
# ----------------------------------------------------------------------- #
def test_extract_text_magic_byte_routes_to_pdf():
    # %PDF- header but a .bin name → the PDF branch (which yields "" on a corrupt
    # PDF), NOT the text decoder (which would return the literal "%PDF-1.4 …" text).
    assert extract_text("mystery.bin", b"%PDF-1.4\nnot really a pdf") == ""


def test_extract_text_plain_still_decodes():
    assert "plain body" in extract_text("note.txt", b"plain body")


# ----------------------------------------------------------------------- #
# chunk_body — upload gets the SAME treatment as ingest
# ----------------------------------------------------------------------- #
def test_chunk_body_html_strips_tags():
    joined = " ".join(
        chunk_body("<h1>Topic</h1><p>Hello <b>world</b> here</p>", content_type="html")
    )
    assert "world" in joined and "Topic" in joined
    assert "<b>" not in joined and "<h1>" not in joined  # tags stripped, not fed raw


def test_chunk_body_markdown_is_heading_aware():
    pieces = chunk_body("# Setup\n\nRun the installer first.", content_type="markdown")
    assert pieces and pieces[0].startswith("Setup")  # heading carried into the chunk


def test_chunk_body_text_keeps_hash_literally():
    # Plain text must NOT treat a leading "#" as a heading (a .txt/PDF "# 1" is data).
    joined = " ".join(chunk_body("# 1 not a heading\nmore", content_type="text"))
    assert "# 1 not a heading" in joined


# ----------------------------------------------------------------------- #
# Directory crawl — skip hidden / VCS / dependency dirs
# ----------------------------------------------------------------------- #
def test_crawl_skips_hidden_and_junk_dirs(tmp_path):
    (tmp_path / "good.md").write_text("# Hi\n\nreal content here")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config.md").write_text("secret")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "dep.md").write_text("a dependency readme")
    (tmp_path / ".hidden.md").write_text("a dotfile")

    src = FileKBSource(SimpleNamespace(source_dir=str(tmp_path), file_max_bytes=0))
    ids = [d.id for d in src.iter_pages()]
    assert ids == ["good.md"]  # only the real file; junk/hidden skipped


# ----------------------------------------------------------------------- #
# Webex download — size cap (stream-enforced, no full buffer first)
# ----------------------------------------------------------------------- #
class _FakeStream:
    def __init__(self, status, headers, chunks):
        self.status_code, self.headers, self._chunks = status, headers, chunks

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_bytes(self):
        yield from self._chunks


def _patch_stream(monkeypatch, headers, chunks, status=200):
    monkeypatch.setattr(httpx, "stream", lambda *a, **k: _FakeStream(status, headers, chunks))


def test_download_within_cap_returns_bytes(monkeypatch):
    _patch_stream(
        monkeypatch,
        {"Content-Length": "5", "Content-Disposition": 'attachment; filename="a.txt"'},
        [b"hel", b"lo"],
    )
    assert download_file("tok", "http://x", max_bytes=100) == ("a.txt", b"hello")


def test_download_rejected_by_content_length(monkeypatch):
    _patch_stream(monkeypatch, {"Content-Length": "9999"}, [b"x"])
    assert download_file("tok", "http://x", max_bytes=10) is None


def test_download_rejected_mid_stream_without_content_length(monkeypatch):
    # No Content-Length header → the cap must still fire while streaming.
    _patch_stream(monkeypatch, {}, [b"x" * 8, b"x" * 8])
    assert download_file("tok", "http://x", max_bytes=10) is None


def test_download_no_cap_allows_large(monkeypatch):
    _patch_stream(monkeypatch, {"Content-Length": "9999"}, [b"x" * 50])
    name, data = download_file("tok", "http://x", max_bytes=0)
    assert name == "document" and data == b"x" * 50

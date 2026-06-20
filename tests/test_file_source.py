"""FileKBSource (files as a knowledge source) + plain-text chunking."""

from types import SimpleNamespace

from kai.pipeline.chunk import chunk_document
from kai.providers.file_source import CompositeKBSource, FileKBSource


def _src(d):
    return FileKBSource(SimpleNamespace(source_dir=str(d)))


def test_file_source_reads_text_and_markdown(tmp_path):
    (tmp_path / "notes.txt").write_text("VPN reset: call IT at extension 4040.")
    (tmp_path / "guide.md").write_text("# Title\n\nThe deploy step uses kubectl.")
    (tmp_path / "ignore.png").write_bytes(b"\x89PNG")  # unsupported -> skipped
    docs = {d.title: d for d in _src(tmp_path).iter_pages()}
    assert set(docs) == {"notes", "guide"}
    assert docs["notes"].content_type == "text"
    assert docs["notes"].url.startswith("file://")
    assert "VPN reset" in docs["notes"].html


def test_text_content_is_not_html_mangled(tmp_path):
    # Plain text with '<' and '&' must survive (html cleaning would corrupt it).
    (tmp_path / "math.txt").write_text("If a < b and x & y hold, then proceed.")
    doc = next(iter(_src(tmp_path).iter_pages()))
    chunks = chunk_document(doc)
    body = " ".join(c.text for c in chunks)
    assert "a < b" in body and "x & y" in body


def test_empty_file_skipped(tmp_path):
    (tmp_path / "blank.txt").write_text("   \n  ")
    assert list(_src(tmp_path).iter_pages()) == []


def test_markdown_headings_chunked(tmp_path):
    (tmp_path / "doc.md").write_text("## Overview\nKafka is a log.\n\n## Detail\nIt scales.")
    doc = next(iter(_src(tmp_path).iter_pages()))
    chunks = chunk_document(doc)
    assert any("Kafka is a log" in c.text for c in chunks)


def test_composite_source_chains(tmp_path):
    (tmp_path / "a.txt").write_text("alpha content")
    (tmp_path / "b.txt").write_text("beta content")
    one = _src(tmp_path)
    titles = {d.title for d in CompositeKBSource(one).iter_pages()}
    assert titles == {"a", "b"}


def test_utf16_file_decoded(tmp_path):
    # UTF-16 (with BOM) is common on Windows — must decode, not ingest NUL garbage.
    (tmp_path / "u16.txt").write_bytes("Café costs 5 EUR".encode("utf-16"))
    docs = {d.title: d for d in _src(tmp_path).iter_pages()}
    assert "u16" in docs and "Café costs" in docs["u16"].html
    assert "\x00" not in docs["u16"].html


def test_cp1252_accents_preserved(tmp_path):
    (tmp_path / "win.txt").write_bytes("Café résumé naïve".encode("cp1252"))
    docs = {d.title: d for d in _src(tmp_path).iter_pages()}
    assert "win" in docs and "Café résumé" in docs["win"].html


def test_binary_file_skipped(tmp_path):
    # A binary blob with a .txt extension must be skipped, not embedded as noise.
    (tmp_path / "blob.txt").write_bytes(bytes(range(256)) * 4)
    assert list(_src(tmp_path).iter_pages()) == []


def test_markdown_vs_plaintext_content_type(tmp_path):
    (tmp_path / "a.md").write_text("# Title\n\nbody")
    (tmp_path / "b.txt").write_text("# not a heading\nbody")
    docs = {d.title: d for d in _src(tmp_path).iter_pages()}
    assert docs["a"].content_type == "markdown"
    assert docs["b"].content_type == "text"


def test_plaintext_hash_line_not_split_as_heading(tmp_path):
    # A .txt line starting with "# " must stay in the body (plain chunking), not be
    # consumed as a section heading the way markdown would.
    (tmp_path / "p.txt").write_text("# shopping list\nmilk\neggs")
    doc = next(iter(_src(tmp_path).iter_pages()))
    body = " ".join(c.text for c in chunk_document(doc))
    assert "# shopping list" in body and "milk" in body


def test_file_size_cap_skips_large_files(tmp_path):
    (tmp_path / "big.txt").write_text("x" * 5000)
    (tmp_path / "small.txt").write_text("hello world")
    src = FileKBSource(SimpleNamespace(source_dir=str(tmp_path), file_max_bytes=1000))
    assert {d.title for d in src.iter_pages()} == {"small"}


def test_file_source_requires_dir():
    import pytest

    with pytest.raises(ValueError):
        FileKBSource(SimpleNamespace(source_dir=""))
    with pytest.raises(ValueError):
        FileKBSource(SimpleNamespace(source_dir="/no/such/dir/xyz123"))

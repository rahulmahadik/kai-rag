"""Local-file knowledge-base source (PDF, Markdown, text, HTML).

:class:`FileKBSource` implements the :class:`~kai.interfaces.KBSource` protocol by
walking a directory and yielding one :class:`~kai.interfaces.Doc` per supported
file. It is the second source type beside Confluence — the rest of the pipeline
(chunk → embed → store → retrieve → answer, and every grounding guard) is
source-agnostic, so files flow through unchanged.

Text is extracted in this module so the chunker receives clean, plain text:

* ``.pdf``               → text via :mod:`pypdf` (lazy import), one blank line per page;
* ``.md/.markdown/.rst`` → read as-is (markdown headings are honoured by the chunker);
* ``.txt/.text/.log``    → read as-is;
* ``.html/.htm``         → read as-is and marked ``content_type="html"`` so the
                           Confluence HTML cleaner strips the tags.

:class:`CompositeKBSource` chains several sources so a deployment can ingest
Confluence **and** files together.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable, Iterator

from kai.interfaces import Doc, KBSource

logger = logging.getLogger("kai.files")

# Markdown gets heading-aware chunking; plain text is windowed without heading
# semantics (so a ``.txt`` line starting with "# " is not treated as a heading).
_MARKDOWN_EXTS = {".md", ".markdown", ".rst"}
_PLAINTEXT_EXTS = {".txt", ".text", ".log"}
_HTML_EXTS = {".html", ".htm"}
_PDF_EXTS = {".pdf"}
_SUPPORTED = _MARKDOWN_EXTS | _PLAINTEXT_EXTS | _HTML_EXTS | _PDF_EXTS

# Above this fraction of replacement/NUL/control chars, decoded bytes are treated
# as binary/garbage and the file is skipped rather than embedded as noise.
_GARBAGE_RATIO = 0.05

# Common binary / office / archive / media types we have NO parser for. Used to
# reject an upload with a CLEAR "unsupported format" message instead of decoding it
# to garbage and answering with a vague "couldn't read any text".
_UNSUPPORTED_UPLOAD_EXTS = {
    ".docx",
    ".doc",
    ".xlsx",
    ".xls",
    ".pptx",
    ".ppt",
    ".odt",
    ".ods",
    ".odp",
    ".rtf",
    ".pages",
    ".numbers",
    ".key",
    ".epub",
    ".zip",
    ".tar",
    ".gz",
    ".tgz",
    ".bz2",
    ".7z",
    ".rar",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".bmp",
    ".tif",
    ".tiff",
    ".webp",
    ".svg",
    ".ico",
    ".mp3",
    ".wav",
    ".flac",
    ".ogg",
    ".mp4",
    ".mov",
    ".avi",
    ".mkv",
    ".webm",
    ".exe",
    ".dll",
    ".so",
    ".dylib",
    ".bin",
    ".woff",
    ".woff2",
    ".ttf",
    ".otf",
}

# Directories never worth crawling (VCS metadata, dependency/build caches, IDE dirs).
_SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".bzr",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".idea",
    ".vscode",
    "dist",
    "build",
}

# Human-readable list of what a dropped file CAN be (shared by chat + API messages).
SUPPORTED_UPLOAD_HINT = "I can read PDF, plain-text, Markdown, and HTML files."


def extract_text(filename: str, data: bytes) -> str:
    """Extract plain text from in-memory file ``data`` (PDF via pypdf, else decode).

    Reused by ad-hoc document Q&A (a file dropped in chat) so it shares the exact
    extraction the ingest path uses. Returns "" when nothing is extractable. A real
    PDF is detected by its ``%PDF-`` magic bytes even if the filename lies.
    """

    ext = Path(filename or "").suffix.lower()
    if ext in _PDF_EXTS or data[:5] == b"%PDF-":  # trust magic bytes over the name
        import io

        try:
            from pypdf import PdfReader
        except ModuleNotFoundError:  # pragma: no cover - dep guard
            return ""
        try:
            reader = PdfReader(io.BytesIO(data))
            return "\n\n".join((pg.extract_text() or "") for pg in reader.pages)
        except Exception:  # noqa: BLE001 — a corrupt PDF yields no text, not a crash
            return ""
    return _decode_text(data) or ""


def content_type_for(filename: str) -> str:
    """Map an uploaded filename to the chunker content_type (html / markdown / text).

    Lets the upload path chunk an ``.html``/``.md`` exactly like the ingest path
    (tag-strip / heading-aware); PDF text and unknown types are plain ``text``.
    """

    ext = Path(filename or "").suffix.lower()
    if ext in _HTML_EXTS:
        return "html"
    if ext in _MARKDOWN_EXTS:
        return "markdown"
    return "text"


def is_unsupported_upload(filename: str) -> bool:
    """True for a file extension we have no parser for (office/image/archive/media)."""

    return Path(filename or "").suffix.lower() in _UNSUPPORTED_UPLOAD_EXTS


def unreadable_reason(filename: str) -> str:
    """A clear, format-aware message for a file we couldn't get any text from.

    Distinguishes "unsupported format" (e.g. .docx) from "a PDF with no text layer"
    (scanned/image → needs OCR) from a generic empty/garbage file — so the user knows
    whether to convert it, run OCR, or that it's simply empty.
    """

    ext = Path(filename or "").suffix.lower()
    if is_unsupported_upload(filename):
        return f"I can't read **{ext or 'that'}** files. {SUPPORTED_UPLOAD_HINT}"
    if ext in _PDF_EXTS:
        return (
            f"I couldn't read any text from **{filename}** — it looks like a scanned "
            "or image-only PDF, which would need OCR first."
        )
    return f"I couldn't find any readable text in **{filename}**. {SUPPORTED_UPLOAD_HINT}"


def _decode_text(raw: bytes) -> str | None:
    """Decode file bytes to clean text, or return ``None`` if it isn't really text.

    Tries UTF-16 (when a BOM is present), then UTF-8 (BOM-aware), then CP1252 as a
    last resort. Rejects results that are mostly replacement chars / NULs / control
    bytes — so a binary file with a ``.txt`` extension, or a UTF-16 export read as
    UTF-8, is skipped instead of silently polluting the vector index with garbage.
    """

    if not raw:
        return ""
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):  # UTF-16 LE/BE BOM
        try:
            return raw.decode("utf-16")
        except UnicodeDecodeError:
            pass
    for enc in ("utf-8-sig", "utf-8"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    # Last resort: CP1252 (superset of latin-1) with replacement, then validate.
    text = raw.decode("cp1252", errors="replace")
    if not text:
        return None
    bad = sum(1 for c in text if c == "�" or (ord(c) < 32 and c not in "\t\n\r"))
    if bad / len(text) > _GARBAGE_RATIO:
        return None
    return text.replace("\x00", "")


class FileKBSource:
    """Yield local files (PDF / Markdown / text / HTML) as :class:`Doc` objects."""

    def __init__(self, settings) -> None:  # noqa: ANN001 — Settings, avoid import cycle
        source_dir = str(getattr(settings, "source_dir", "") or "").strip()
        if not source_dir:
            raise ValueError(
                "FileKBSource requires SOURCE_DIR to be set (the directory of files to ingest)."
            )
        root = Path(source_dir).expanduser()
        if not root.is_dir():
            raise ValueError(f"SOURCE_DIR {source_dir!r} is not a directory (resolved to {root}).")
        self._root = root
        self._max_bytes = int(getattr(settings, "file_max_bytes", 0) or 0)
        # doc_ids ENCOUNTERED in the last crawl (yielded OR skipped for size/empty),
        # so prune keeps a file that's present-but-skipped instead of deleting it.
        self.seen_ids: set[str] = set()
        # Source-level crawl failures (for the prune partial-crawl guard); uniform with
        # CompositeKBSource so getattr(kb, "errors", 0) is meaningful for any source.
        self.errors = 0

    # ------------------------------------------------------------------
    # KBSource protocol
    # ------------------------------------------------------------------
    def iter_pages(self) -> Iterable[Doc]:
        """Yield one :class:`Doc` per supported file under SOURCE_DIR (recursive)."""

        self.seen_ids = set()  # reset per crawl
        for path in sorted(self._root.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in _SUPPORTED:
                continue
            if self._skip(path):  # hidden / VCS / build-cache dirs and dotfiles
                continue
            # The file EXISTS upstream — record it as seen before any skip below, so a
            # too-large / empty file is never pruned as if it had been deleted.
            self.seen_ids.add(path.relative_to(self._root).as_posix())
            # Size cap (bytes, not pages) — skip a too-large file before reading it
            # into memory, with a clear log so the operator can raise the limit.
            if self._max_bytes > 0:
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                if size > self._max_bytes:
                    logger.warning(
                        "kai_file_too_large path=%s size=%dB limit=%dB (raise FILE_MAX_BYTES to ingest)",
                        path.name,
                        size,
                        self._max_bytes,
                    )
                    continue
            try:
                doc = self._to_doc(path)
            except Exception as exc:  # noqa: BLE001 — one bad file must not fail the run
                logger.warning("kai_file_skipped path=%s err=%s", path.name, type(exc).__name__)
                continue
            if doc is not None:
                yield doc

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _skip(self, path: Path) -> bool:
        """Skip dotfiles and hidden/junk directories (``.git``, ``node_modules``,
        build caches) so a crawl never ingests VCS metadata or dependencies — the
        extension allowlist alone would otherwise let a ``node_modules/*.md`` through.
        """

        for part in path.relative_to(self._root).parts:
            if part.startswith(".") or part in _SKIP_DIRS:
                return True
        return False

    def _to_doc(self, path: Path) -> Doc | None:
        ext = path.suffix.lower()
        if ext in _PDF_EXTS:
            body, ctype = self._read_pdf(path), "text"
            if not body or not body.strip():
                # A PDF with no extractable text is almost always scanned/image-only
                # — say so explicitly rather than skip silently (it would need OCR).
                logger.warning("kai_pdf_no_text path=%s (scanned/image PDF? needs OCR)", path.name)
                return None
        else:
            body = _decode_text(path.read_bytes())
            if body is None:
                # Undecodable / binary content with a text extension — skip rather
                # than embed U+FFFD/NUL garbage that would pollute the index.
                logger.warning(
                    "kai_file_undecodable path=%s (binary or unknown encoding)", path.name
                )
                return None
            if ext in _HTML_EXTS:
                ctype = "html"
            elif ext in _MARKDOWN_EXTS:
                ctype = "markdown"  # heading-aware chunking
            else:
                ctype = "text"  # plain text — no heading semantics

        if not body or not body.strip():
            return None  # empty -> skip rather than ingest blank

        rel = path.relative_to(self._root).as_posix()
        return Doc(
            id=rel,  # stable, unique -> idempotent chunk ids
            title=path.stem,
            url=path.resolve().as_uri(),  # file:///… so citations are clickable
            html=body,
            space="files",
            content_type=ctype,
            updated="",
        )

    @staticmethod
    def _read_pdf(path: Path) -> str:
        """Extract text from a PDF, one blank line between pages."""

        try:
            from pypdf import PdfReader
        except ModuleNotFoundError as exc:  # pragma: no cover - dep guard
            raise RuntimeError(
                "Reading PDFs needs the 'pypdf' package — run `.venv/bin/pip install pypdf`."
            ) from exc

        reader = PdfReader(str(path))
        pages: list[str] = []
        for i, page in enumerate(reader.pages):
            try:
                pages.append(page.extract_text() or "")
            except Exception as exc:  # noqa: BLE001 — one bad page must not lose the file
                logger.warning(
                    "kai_pdf_page_skipped path=%s page=%d err=%s", path.name, i, type(exc).__name__
                )
        return "\n\n".join(pages)


class CompositeKBSource:
    """Chain several :class:`KBSource` objects into one (e.g. Confluence + files)."""

    def __init__(self, *sources: KBSource) -> None:
        self._sources = [s for s in sources if s is not None]
        self.errors = 0  # sources that failed during the most recent iter_pages()

    @property
    def seen_ids(self) -> set[str]:
        """Union of every child's seen ids (yielded OR skipped) — for prune safety."""

        out: set[str] = set()
        for source in self._sources:
            out |= getattr(source, "seen_ids", set()) or set()
        return out

    def iter_pages(self) -> Iterator[Doc]:
        self.errors = 0
        for source in self._sources:
            label = (
                getattr(source, "_base_url", None)
                or getattr(source, "_root", None)
                or type(source).__name__
            )
            count = 0
            try:
                for doc in source.iter_pages():
                    count += 1
                    yield doc
            except Exception as exc:  # noqa: BLE001 — one bad source must not kill the rest
                self.errors += 1
                logger.error(
                    "kai_source_failed source=%s yielded=%d err=%s: %s — skipping the "
                    "rest of this source; remaining sources continue.",
                    label,
                    count,
                    type(exc).__name__,
                    exc,
                )
                continue
            logger.info("kai_source_done source=%s docs=%d", label, count)

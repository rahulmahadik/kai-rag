"""Confluence-storage HTML → clean text → overlapping :class:`Chunk` slices.

The chunker is the bridge between a raw :class:`~kai.interfaces.Doc` (carrying
Confluence *storage format* HTML) and the retrievable :class:`~kai.interfaces.Chunk`
units the vector store indexes. It:

1. strips Confluence storage macros (``<ac:...>``, ``<ri:...>``) and ordinary
   HTML tags, unescaping entities, into readable plain text; and
2. splits that text into ~500-token windows with a small overlap so context that
   straddles a boundary is not lost, assigning each window a **stable** id of the
   form ``"{doc_id}#{ordinal}"``.

Only the standard library is used here (regex + ``html``).
"""

from __future__ import annotations

import html as _html
import re

from kai.interfaces import Chunk, Doc

# Approximate target/overlap sizing, measured in whitespace tokens. These are
# deliberately coarse — exact tokenisation is the model's job; we only need
# chunks small enough to embed and large enough to be self-contained.
_TARGET_TOKENS = 500
_OVERLAP_TOKENS = 60

# Block-level tags whose boundaries should become paragraph breaks so sentences
# from adjacent blocks don't run together once tags are stripped.
_BLOCK_BREAK_RE = re.compile(
    r"</(?:p|div|li|h[1-6]|tr|table|ul|ol|blockquote|pre)\s*>",
    re.IGNORECASE,
)
# A list item / line-break open tag → newline so list bullets stay on their own line.
_BLOCK_OPEN_RE = re.compile(r"<(?:li|br)\b[^>]*>", re.IGNORECASE)
# A table-cell close → " | " so cells within a row stay separated once tags are
# stripped (otherwise <td>9092</td><td>broker-1</td> collapses to "9092 broker-1"
# and the value↔column mapping is lost). Row boundaries (</tr>) already become
# newlines via _BLOCK_BREAK_RE, so a row renders as "9092 | broker-1".
_CELL_BREAK_RE = re.compile(r"</(?:td|th)\s*>", re.IGNORECASE)
# Headings → markdown-style section markers ("## Title") so the chunker can split
# on section boundaries and keep each heading's terms attached to its body (which
# helps a query that matches the heading retrieve the right section).
_HEADING_RE = re.compile(r"<h([1-6])\b[^>]*>(.*?)</h\1>", re.IGNORECASE | re.DOTALL)
# A heading marker as it appears in the cleaned text (start of a line).
_HEADING_LINE_RE = re.compile(r"^#{1,6}\s+(.+)$")
# Confluence structured macros, inline-comment markers and resource tags.
_MACRO_RE = re.compile(r"</?(?:ac|ri):[^>]*>", re.IGNORECASE)
# Whole non-content blocks (tag + body) for real-web HTML files: script/style/head
# bodies and comments — removed before generic tag stripping so their text doesn't leak.
_SCRIPT_STYLE_RE = re.compile(r"<(script|style|head)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
# Any remaining HTML/XML tag.
_TAG_RE = re.compile(r"<[^>]+>")
# Collapse runs of blank lines / whitespace.
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")
_MULTI_SPACE_RE = re.compile(r"[ \t]{2,}")


def _heading_sub(m: "re.Match[str]") -> str:
    """Replace an ``<h1-6>…</h1-6>`` element with a ``## Title`` marker line."""

    level = int(m.group(1))
    inner = _TAG_RE.sub(" ", m.group(2))
    inner = _MULTI_SPACE_RE.sub(" ", _html.unescape(inner)).strip()
    return f"\n\n{'#' * level} {inner}\n" if inner else "\n\n"


def html_to_text(html: str) -> str:
    """Convert Confluence storage HTML to clean, readable plain text.

    Macros and tags are removed, block boundaries become newlines, headings
    become ``## Title`` marker lines, and HTML entities are unescaped. The result
    preserves paragraph + section structure (used by the header-aware chunker)
    without any markup.
    """

    if not html:
        return ""

    text = html
    # Drop non-content blocks WHOLE (tag + body) before anything else. Confluence
    # storage HTML has none of these, but real-web .html files do — and the generic
    # tag strip below would otherwise leave the JS/CSS/comment TEXT behind.
    text = _SCRIPT_STYLE_RE.sub(" ", text)
    text = _COMMENT_RE.sub(" ", text)
    # Headings first → "## Title" markers (before tags are stripped generically).
    text = _HEADING_RE.sub(_heading_sub, text)
    # Turn block closes/opens into explicit breaks before stripping tags.
    # Cell closes first (→ " | "), so the later </tr> → "\n" leaves one row per line
    # with pipe-separated cells; otherwise the generic tag strip would merge cells.
    text = _CELL_BREAK_RE.sub(" | ", text)
    text = _BLOCK_BREAK_RE.sub("\n", text)
    text = _BLOCK_OPEN_RE.sub("\n", text)
    # Drop Confluence structured-macro / resource tags, then any other tag.
    text = _MACRO_RE.sub(" ", text)
    text = _TAG_RE.sub(" ", text)
    # Unescape entities (&amp; &lt; &nbsp; …).
    text = _html.unescape(text)
    return _normalise_ws(text)


def _normalise_ws(text: str) -> str:
    """Collapse intra-line space runs, trim each line, squeeze blank-line runs.

    Shared by :func:`html_to_text` and the plain-text path so file-sourced content
    (PDF/markdown/txt) gets the same whitespace tidy-up WITHOUT any tag stripping
    or entity handling (which would corrupt prose containing ``<`` or ``&``).
    """

    lines = [ln.strip() for ln in (text or "").split("\n")]
    text = "\n".join(lines)
    text = _MULTI_SPACE_RE.sub(" ", text)
    text = _MULTI_NEWLINE_RE.sub("\n\n", text)
    return text.strip()


def _split_tokens(text: str) -> list[str]:
    """Whitespace token split — the unit our window sizing counts in."""

    return text.split()


def chunk_text(
    text: str,
    *,
    target_tokens: int = _TARGET_TOKENS,
    overlap_tokens: int = _OVERLAP_TOKENS,
) -> list[str]:
    """Split ``text`` into ~``target_tokens`` windows with ``overlap_tokens`` overlap.

    Windows are built over whitespace tokens then rejoined to text. The overlap
    keeps a tail of the previous window at the head of the next so a fact that
    spans a boundary is still retrievable from at least one chunk.
    """

    if target_tokens <= 0:
        raise ValueError(f"target_tokens must be positive, got {target_tokens}")
    if overlap_tokens < 0 or overlap_tokens >= target_tokens:
        raise ValueError(
            f"overlap_tokens must satisfy 0 <= overlap < target, got "
            f"{overlap_tokens} (target={target_tokens})"
        )

    tokens = _split_tokens(text)
    if not tokens:
        return []
    if len(tokens) <= target_tokens:
        return [" ".join(tokens)]

    step = target_tokens - overlap_tokens
    windows: list[str] = []
    start = 0
    # Bounded loop: ``step`` is strictly positive, so ``start`` always advances.
    while start < len(tokens):
        window = tokens[start : start + target_tokens]
        windows.append(" ".join(window))
        if start + target_tokens >= len(tokens):
            break
        start += step
    return windows


def _split_sections(text: str) -> list[tuple[str | None, str]]:
    """Partition cleaned text into ``(heading, body)`` sections at heading markers.

    Content before the first heading is a leading ``(None, body)`` section.
    Fully-empty sections are dropped.
    """

    sections: list[tuple[str | None, str]] = []
    heading: str | None = None
    lines: list[str] = []
    in_fence = False  # inside a ``` / ~~~ fenced code block
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            lines.append(line)
            continue
        # A "# ..." line inside a fenced code block is a code comment, not a heading.
        m = None if in_fence else _HEADING_LINE_RE.match(stripped)
        if m:
            sections.append((heading, "\n".join(lines).strip()))
            heading = m.group(1).strip()
            lines = []
        else:
            lines.append(line)
    sections.append((heading, "\n".join(lines).strip()))
    return [(h, b) for h, b in sections if (h is not None or b)]


def chunk_text_headers(
    text: str,
    *,
    target_tokens: int = _TARGET_TOKENS,
    overlap_tokens: int = _OVERLAP_TOKENS,
) -> list[str]:
    """Header-aware split: break at heading boundaries, size-cap each section, and
    prefix every emitted piece with its section heading.

    A section that fits in ``target_tokens`` becomes one chunk (``heading + body``);
    a larger section is windowed (with overlap) and the heading is re-attached to
    each window so the section title rides into every embedding. Text with no
    headings degrades gracefully to plain size-based windows.
    """

    if not text.strip():
        return []
    pieces: list[str] = []
    for heading, body in _split_sections(text):
        prefix = f"{heading}\n" if heading else ""
        windows = (
            chunk_text(body, target_tokens=target_tokens, overlap_tokens=overlap_tokens)
            if body.strip()
            else [""]
        )
        for window in windows:
            piece = (prefix + window).strip()
            if piece:
                pieces.append(piece)
    return pieces


def chunk_body(
    body: str,
    *,
    content_type: str = "text",
    target_tokens: int = _TARGET_TOKENS,
    overlap_tokens: int = _OVERLAP_TOKENS,
) -> list[str]:
    """Clean and window a raw document body into text pieces, routed by content_type.

    Shared by :func:`chunk_document` (corpus ingest) and
    :func:`kai.pipeline.ask.answer_from_document` (ad-hoc upload) so the SAME bytes
    get the SAME treatment on either path — an uploaded ``.html`` is tag-stripped and
    markdown gets heading-aware chunking, instead of the LLM seeing raw markup.

    Routing by content_type:

    * ``html``     → strip macros/tags/script-style, then heading-aware windows;
    * ``markdown`` → verbatim (never HTML-mangled), heading-aware windows;
    * ``text``     → verbatim, PLAIN size-based windows (no ``# `` heading semantics,
      so a ``.txt``/PDF line starting with ``#`` isn't split as a heading).
    """

    text = html_to_text(body) if content_type == "html" else _normalise_ws(body)
    if content_type == "text":
        return chunk_text(text, target_tokens=target_tokens, overlap_tokens=overlap_tokens)
    return chunk_text_headers(text, target_tokens=target_tokens, overlap_tokens=overlap_tokens)


def chunk_document(
    doc: Doc,
    *,
    target_tokens: int = _TARGET_TOKENS,
    overlap_tokens: int = _OVERLAP_TOKENS,
) -> list[Chunk]:
    """Clean and split a single :class:`Doc` into stable-id :class:`Chunk` slices.

    Uses header-aware chunking (sections kept coherent, headings carried into each
    chunk). Returns an empty list when the document has no extractable text. Each
    chunk carries the document's title/url/space metadata so citations and filters
    work without a join back to the source document.
    """

    pieces = chunk_body(
        doc.html,
        content_type=getattr(doc, "content_type", "html"),
        target_tokens=target_tokens,
        overlap_tokens=overlap_tokens,
    )
    chunks: list[Chunk] = []
    title = (doc.title or "").strip()
    for ordinal, piece in enumerate(pieces):
        if not piece.strip():
            continue
        # Prepend the page title to each chunk's text so the title's distinctive
        # terms ride into BOTH the embedding and the lexical index. This is what
        # stops a "BloodHound proposal" query from matching a near-identical
        # "Lucene proposal" chunk — the bodies look alike, the titles disambiguate.
        body = f"{title}\n{piece}" if title else piece
        chunks.append(
            Chunk(
                id=Chunk.make_id(doc.id, ordinal),
                doc_id=doc.id,
                title=doc.title,
                url=doc.url,
                text=body,
                space=doc.space,
                ordinal=ordinal,
            )
        )
    return chunks

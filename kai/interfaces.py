"""KAI core contracts.

This module is the FIXED CONTRACT every other module codes against. The
dataclasses describe the data flowing through the pipeline; the
``typing.Protocol`` classes describe the swappable provider boundaries
(embeddings, LLM, vector store, knowledge-base source, tracker).

Nothing here imports heavy SDKs — it is pure typing so it is always safe
to import.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Protocol, Sequence, runtime_checkable

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class Doc:
    """A raw knowledge-base document (a Confluence page, a PDF, a text file, …)
    before chunking.

    ``html`` holds the document body (the field name is historical — it carries
    Confluence storage-format HTML, but also plain text from file sources).
    ``content_type`` tells the chunker how to clean it: ``"html"`` (strip
    macros/tags) or ``"text"`` (already-plain text from a PDF/markdown/txt source —
    used verbatim, never HTML-stripped).
    """

    id: str
    title: str
    url: str
    html: str
    space: str = ""
    labels: list[str] = field(default_factory=list)
    updated: str = ""
    content_type: str = "html"


@dataclass
class Chunk:
    """A retrievable slice of a :class:`Doc`.

    ``id`` is stable and deterministic: ``f"{doc_id}#{ordinal}"`` so re-ingesting
    the same document produces the same chunk ids (idempotent upserts).
    """

    id: str
    doc_id: str
    title: str
    url: str
    text: str
    space: str = ""
    ordinal: int = 0

    @staticmethod
    def make_id(doc_id: str, ordinal: int) -> str:
        """Build the stable chunk id for a given document/ordinal."""

        return f"{doc_id}#{ordinal}"


@dataclass
class ScoredChunk:
    """A :class:`Chunk` paired with its retrieval relevance score.

    ``score`` is the (rank-based) fusion score used for ordering. ``vector_score``
    is the raw cosine similarity of the chunk vs the query (≈0..1) — an *absolute*
    relevance signal the answer pipeline uses to decide whether the question is
    actually covered by the knowledge base (vs answering off-topic).
    """

    chunk: Chunk
    score: float
    vector_score: float = 0.0
    # Cross-encoder relevance logit, set only when a reranker ran (else None).
    rerank_score: float | None = None


@dataclass
class Citation:
    """A source reference surfaced alongside an answer."""

    title: str
    url: str


@dataclass
class Answer:
    """The final response returned to the caller.

    ``escalated`` is True when KAI could not answer with enough confidence and
    opened a tracker issue; ``escalation_url`` then points at that issue.
    """

    answer: str
    citations: list[Citation]
    confidence: float
    escalated: bool
    escalation_url: str | None = None
    # On escalation: the closest-but-insufficient sources, so the asker can
    # self-serve instead of hitting a dead end (clearly labeled "not an answer"
    # by the caller). Empty on confident answers (those use ``citations``).
    suggested_sources: list[Citation] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Provider boundaries (Protocols)
# ---------------------------------------------------------------------------


@runtime_checkable
class Embedder(Protocol):
    """Turns text into dense vectors."""

    @property
    def dimensions(self) -> int:
        """Dimensionality of the vectors this embedder produces."""
        ...

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed a batch of texts, returning one vector per input text."""
        ...


@runtime_checkable
class LLMClient(Protocol):
    """Chat-completion style language model client."""

    def complete(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int = 1024,
        temperature: float = 0.1,
    ) -> str:
        """Run a single completion and return the assistant text."""
        ...


@runtime_checkable
class VectorStore(Protocol):
    """Persists chunk vectors and retrieves the most relevant chunks."""

    def ensure_schema(self, dimensions: int) -> None:
        """Create the backing table/indexes sized for ``dimensions`` vectors."""
        ...

    def upsert(
        self,
        chunks: Sequence[Chunk],
        vectors: Sequence[Sequence[float]],
    ) -> None:
        """Insert or replace chunks together with their vectors."""
        ...

    def search(
        self,
        query_vector: Sequence[float],
        query_text: str,
        top_k: int,
        filters: dict | None = None,
    ) -> list[ScoredChunk]:
        """Return up to ``top_k`` chunks most relevant to the query."""
        ...

    def delete(self, doc_id: str) -> None:
        """Remove every chunk belonging to ``doc_id``."""
        ...

    def list_doc_ids(self) -> list[str]:
        """Return the distinct ``doc_id`` values currently stored.

        Used by ingestion to reconcile the store against the live source: any
        ``doc_id`` present here but absent from the latest crawl has been removed
        upstream and should be deleted.
        """
        ...


@runtime_checkable
class KBSource(Protocol):
    """A source of knowledge-base documents (e.g. a Confluence space)."""

    def iter_pages(self) -> Iterable[Doc]:
        """Yield every document available from this source."""
        ...


@runtime_checkable
class Tracker(Protocol):
    """An issue tracker used for escalations (e.g. Jira)."""

    def create_issue(self, title: str, body: str) -> str:
        """Create an issue and return its browsable URL."""
        ...

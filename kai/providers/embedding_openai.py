"""OpenAI-compatible embeddings provider.

A thin, dependency-light client that talks to any service exposing the
OpenAI ``/embeddings`` REST shape (OpenAI itself, a local ``llama.cpp``
server, vLLM, Ollama's OpenAI bridge, etc.). It performs a single plain
``httpx`` POST and returns one vector per input text.

The class fails loudly:

* missing ``base_url`` / ``api_key`` / ``model`` at construction time;
* a non-200 HTTP response (with the status code, never the raw body);
* a malformed response that does not contain the expected ``data`` array;
* a vector whose length disagrees with the configured ``dimensions``.

This module is loaded only when the real embedder is used.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence

import httpx

from kai.config import Settings


class OpenAIEmbedder:
    """Embeds text via an OpenAI-compatible ``/embeddings`` endpoint."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        dimensions: int,
        timeout: int = 120,
    ) -> None:
        if not base_url.strip():
            raise ValueError(
                "OpenAIEmbedder requires a non-empty embed_base_url (set EMBED_BASE_URL)."
            )
        if not api_key.strip():
            raise ValueError(
                "OpenAIEmbedder requires a non-empty embed_api_key (set EMBED_API_KEY)."
            )
        if not model.strip():
            raise ValueError("OpenAIEmbedder requires a non-empty embed_model (set EMBED_MODEL).")
        if dimensions <= 0:
            raise ValueError(
                f"OpenAIEmbedder requires a positive embed_dimensions, got {dimensions!r}."
            )

        # Normalise so we can safely append "/embeddings".
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._dimensions = int(dimensions)
        self._timeout = int(timeout)
        # Lazy, reusable client (HTTP keep-alive). Locked: handlers run in a
        # threadpool, so an unguarded build leaks the loser's connections.
        self._http: httpx.Client | None = None
        self._http_lock = threading.Lock()

    def _client(self) -> httpx.Client:
        if self._http is None:
            with self._http_lock:
                if self._http is None:
                    self._http = httpx.Client(
                        timeout=self._timeout,
                        limits=httpx.Limits(max_connections=32, max_keepalive_connections=8),
                    )
        return self._http

    def close(self) -> None:
        """Release the pooled connections. Idempotent; safe to call on a fresh client."""

        with self._http_lock:
            if self._http is not None:
                self._http.close()
                self._http = None

    @classmethod
    def from_settings(cls, settings: Settings) -> OpenAIEmbedder:
        """Build an embedder from a :class:`~kai.config.Settings` instance."""

        return cls(
            base_url=settings.embed_base_url,
            api_key=settings.embed_api_key,
            model=settings.embed_model,
            dimensions=settings.embed_dimensions,
            # Reuse the LLM timeout knob, embeddings share the same budget.
            timeout=settings.llm_timeout,
        )

    @property
    def dimensions(self) -> int:
        """Dimensionality of the vectors this embedder produces."""

        return self._dimensions

    # When a short-context model (e.g. mxbai-embed-large's 512 tokens) rejects an
    # over-long input with a 4xx, we retry that single input truncated to this
    # many characters (~one model window) rather than failing the whole ingest.
    _MAX_CHARS_ON_RETRY = 1500

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed a batch of texts, returning one vector per input text.

        Resilient to a short-context embedder rejecting an over-long chunk: on a
        4xx the batch is retried one item at a time, and any single item still
        rejected is truncated and retried, so one oversized chunk cannot fail an
        entire ingest. Network errors, 5xx and malformed responses still raise.
        """

        items = list(texts)
        if not items:
            return []

        ok, vectors, status = self._post(items)
        if ok:
            return vectors

        # Batch rejected (4xx). Recover per item.
        out: list[list[float]] = []
        for text in items:
            one_ok, one_vec, _ = self._post([text])
            if not one_ok:
                one_ok, one_vec, _ = self._post([text[: self._MAX_CHARS_ON_RETRY]])
            if not one_ok:
                raise RuntimeError(
                    f"Embedding endpoint {self._base_url}/embeddings rejected an "
                    f"input (HTTP {status}); it failed even after truncation."
                )
            out.append(one_vec[0])
        return out

    def _post(self, items: list[str]) -> tuple[bool, list[list[float]], int]:
        """POST one batch. Returns ``(ok, vectors, status)``.

        ``ok`` is False for a recoverable 4xx (caller retries per item); 5xx,
        network and malformed-response errors raise.
        """

        url = f"{self._base_url}/embeddings"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        try:
            resp = self._client().post(
                url,
                headers=headers,
                json={"model": self._model, "input": items},
            )
        except httpx.HTTPError as exc:  # network/timeout/connection error
            raise RuntimeError(f"Embedding request to {url} failed: {type(exc).__name__}") from exc

        if 400 <= resp.status_code < 500:
            return False, [], resp.status_code
        if resp.status_code != 200:
            raise RuntimeError(f"Embedding endpoint {url} returned HTTP {resp.status_code}.")

        try:
            body = resp.json()
        except ValueError as exc:
            raise RuntimeError(f"Embedding endpoint {url} returned non-JSON response.") from exc

        data = body.get("data")
        if not isinstance(data, list) or len(data) != len(items):
            got = len(data) if isinstance(data, list) else "no"
            raise RuntimeError(
                f"Embedding response from {url} malformed ({got} vectors for {len(items)} inputs)."
            )

        # The OpenAI contract guarantees ordering by 'index'; sort to be safe.
        ordered = sorted(data, key=lambda row: row.get("index", 0))
        vectors: list[list[float]] = []
        for row in ordered:
            vec = row.get("embedding")
            if not isinstance(vec, list):
                raise RuntimeError(f"Embedding response from {url} missing an 'embedding' list.")
            if len(vec) != self._dimensions:
                raise RuntimeError(
                    f"Embedding endpoint {url} returned a vector of length "
                    f"{len(vec)} but embed_dimensions is {self._dimensions}; "
                    "fix EMBED_DIMENSIONS to match the model."
                )
            vectors.append([float(x) for x in vec])
        return True, vectors, 200

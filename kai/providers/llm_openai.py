"""OpenAI-compatible chat-completions LLM provider.

A thin client over any service exposing the OpenAI ``/chat/completions``
REST shape (OpenAI, vLLM, llama.cpp server, Ollama's OpenAI bridge, ...).
It runs a single plain ``httpx`` POST built from a system + user message
and returns the assistant text.

The class fails loudly:

* missing ``base_url`` / ``api_key`` / ``model`` at construction time;
* a non-200 HTTP response (status code only — never the raw body);
* a malformed response that does not contain ``choices[0].message.content``.

This module is loaded only when the real LLM provider is used.
"""

from __future__ import annotations

import httpx

from kai.config import Settings


class OpenAILLM:
    """Chat completions via an OpenAI-compatible ``/chat/completions`` endpoint."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout: int = 120,
    ) -> None:
        if not base_url.strip():
            raise ValueError("OpenAILLM requires a non-empty llm_base_url (set LLM_BASE_URL).")
        if not api_key.strip():
            raise ValueError("OpenAILLM requires a non-empty llm_api_key (set LLM_API_KEY).")
        if not model.strip():
            raise ValueError("OpenAILLM requires a non-empty llm_model (set LLM_MODEL).")

        # Normalise so we can safely append "/chat/completions".
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout = int(timeout)
        # Reusable client → HTTP keep-alive across completions. Created lazily.
        self._http: httpx.Client | None = None

    def _client(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(timeout=self._timeout)
        return self._http

    @classmethod
    def from_settings(cls, settings: Settings) -> "OpenAILLM":
        """Build an LLM client from a :class:`~kai.config.Settings` instance."""

        return cls(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            model=settings.llm_model,
            timeout=settings.llm_timeout,
        )

    def complete(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int = 1024,
        temperature: float = 0.1,
    ) -> str:
        """Run a single completion and return the assistant text."""

        url = f"{self._base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": int(max_tokens),
            "temperature": float(temperature),
        }

        try:
            resp = self._client().post(url, headers=headers, json=payload)
        except httpx.HTTPError as exc:  # network/timeout/connection error
            raise RuntimeError(
                f"Chat completion request to {url} failed: {type(exc).__name__}"
            ) from exc

        if resp.status_code != 200:
            raise RuntimeError(f"Chat endpoint {url} returned HTTP {resp.status_code}.")

        try:
            body = resp.json()
        except ValueError as exc:
            raise RuntimeError(f"Chat endpoint {url} returned non-JSON response.") from exc

        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            raise RuntimeError(f"Chat response from {url} missing a non-empty 'choices' array.")

        message = choices[0].get("message")
        if not isinstance(message, dict):
            raise RuntimeError(f"Chat response from {url} missing 'choices[0].message'.")

        content = message.get("content")
        if not isinstance(content, str):
            raise RuntimeError(f"Chat response from {url} missing 'choices[0].message.content'.")

        return content

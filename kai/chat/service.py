"""Platform-NEUTRAL chat logic: call the KAI API, format the reply, route feedback.

Everything here is portable across Webex/Slack/Teams and unit-testable without any
chat SDK or network (the HTTP calls are the only I/O). Adapters call into a
:class:`ChatService`; they never re-implement this.

The reply markdown is platform-portable; per-platform concerns (message-size
splitting limit, rich-card/Block-Kit format, threading) stay in the adapter.
"""

from __future__ import annotations

import logging

import httpx

from kai.chat.base import FeedbackEvent, IncomingMessage
from kai.config import Settings

logger = logging.getLogger("kai.chat")

# What KAI can do, shown when a user types "help" on any platform. Kept here (not in
# an adapter) so every surface answers "help" identically.
HELP_TEXT = (
    "**I'm KAI** — I answer questions from your team's knowledge base and cite my "
    "sources, and I escalate to a human instead of guessing.\n\n"
    "• **Ask me anything** in plain language — e.g. _“How do I rotate the API keys?”_\n"
    "• **Attach a file** (PDF, text, Markdown, or HTML) and ask about it — I read it "
    "just for that question; it isn't saved to the knowledge base.\n"
    "• Use the **👍 / 👎** buttons on an answer, or ask me to **escalate** if I got it wrong.\n\n"
    "If something isn't in the knowledge base, I'll tell you rather than make it up."
)

# Exact phrases that should show the help card instead of being answered as a question.
_HELP_TRIGGERS = {
    "help",
    "/help",
    "?",
    "help me",
    "commands",
    "/commands",
    "what can you do",
    "what can i ask",
    "how do you work",
    "how does this work",
}


def is_help_request(text: str) -> bool:
    """True when the user is asking what the bot can do (so we show HELP_TEXT)."""

    t = (text or "").strip().lower()
    return t in _HELP_TRIGGERS or t.rstrip("?!.") in _HELP_TRIGGERS


# --------------------------------------------------------------------------- #
# Pure formatting (no network) — re-exported from kai.bot for back-compat.
# --------------------------------------------------------------------------- #
def format_reply(ask_json: dict) -> str:
    """Render a ``/ask`` JSON response as a portable-markdown reply.

    Confident answer → answer + de-duplicated source links. Escalated answer →
    the escalation text + the CLOSEST sources (clearly labeled not-an-answer) so
    the asker can self-serve instead of hitting a dead end.
    """

    answer = (ask_json.get("answer") or "").strip()
    if not answer:
        return "I don't have an answer for that."

    def _links(items: list) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for c in items or []:
            url = (c.get("url") or "").strip()
            title = (c.get("title") or url or "source").strip()
            if url and url not in seen:
                seen.add(url)
                out.append(f"- [{title}]({url})")
        return out

    if ask_json.get("escalated"):
        # Escalations otherwise look like a normal answer on chat surfaces; lead with a
        # glanceable marker so it's unmistakable that KAI did NOT answer (it refused to
        # guess). The pipeline's plain-English explanation follows the marker.
        answer = f"⚠️ {answer}"
        suggested = _links(ask_json.get("suggested_sources") or [])
        if not suggested:
            return answer
        return "\n".join(
            [
                answer,
                "",
                "**Closest pages I found, in case they help** _(not a confirmed answer)_:",
                *suggested,
            ]
        )

    sources = _links(ask_json.get("citations") or [])
    if not sources:
        return answer
    return "\n".join([answer, "", "**Sources:**", *sources])


def split_message(text: str, limit: int) -> list[str]:
    """Split ``text`` into <=``limit``-byte pieces at paragraph/line boundaries.

    Every platform caps message size (Webex 7439 bytes, Slack ~3000/▒block, Teams
    ~28KB); the adapter passes its own ``limit``. Prefers blank-line, then newline,
    then a hard cut, so markdown structure (and a trailing Sources block) survives.
    """

    if not text:
        return []
    if len(text.encode("utf-8")) <= limit:
        return [text]

    pieces: list[str] = []
    rest = text
    while len(rest.encode("utf-8")) > limit:
        window = rest.encode("utf-8")[:limit].decode("utf-8", "ignore")
        cut = window.rfind("\n\n")
        if cut < limit // 4:
            cut = window.rfind("\n")
        if cut < limit // 4:
            cut = len(window)
        pieces.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip("\n")
    if rest.strip():
        pieces.append(rest)
    return pieces


# --------------------------------------------------------------------------- #
# The service: HTTP I/O against the KAI API
# --------------------------------------------------------------------------- #
class ChatService:
    """Drives one question/feedback through KAI's HTTP API. Platform-agnostic."""

    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._base = settings.kai_api_url.rstrip("/")

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self._s.api_key:
            h["Authorization"] = f"Bearer {self._s.api_key}"
        return h

    def answer(self, msg: IncomingMessage) -> tuple[dict | None, str]:
        """POST the question to ``/ask``. Returns ``(json, "")`` or ``(None, friendly_error)``.

        Never raises — distinguishes misconfiguration from outage so users aren't
        told to retry something only an admin can fix.
        """

        try:
            resp = httpx.post(
                self._base + "/ask",
                json={"question": msg.text},
                headers=self._headers(),
                timeout=float(self._s.llm_timeout) + 30.0,
            )
        except httpx.TimeoutException:
            logger.error("kai_ask_failed err=Timeout")
            return None, "Sorry — that took longer than expected. Please try again in a moment."
        except httpx.HTTPError as exc:
            logger.error("kai_ask_failed err=%s", type(exc).__name__)
            return None, (
                "Sorry — I couldn't reach the knowledge service just now. "
                "Please try again in a moment."
            )
        if resp.status_code in (401, 403):
            logger.error("kai_ask_failed status=%d (auth)", resp.status_code)
            return None, (
                "I'm not configured correctly (API authentication failed) "
                "— please tell an administrator."
            )
        if resp.status_code == 422:
            return None, "I couldn't process that question — try rephrasing it."
        if resp.status_code != 200:
            logger.error("kai_ask_failed status=%d", resp.status_code)
            return None, (
                "Sorry — the knowledge service had a problem with that "
                "question. Please try again in a moment."
            )
        try:
            return resp.json(), ""
        except ValueError:
            logger.error("kai_ask_failed err=BadJSON")
            return None, "Sorry — I got a malformed reply from the knowledge service."

    def ask_document(self, filename: str, data: bytes, question: str) -> tuple[dict | None, str]:
        """POST an uploaded file's bytes to ``/ask-document`` for ad-hoc RAG."""

        import base64

        payload = {
            "question": question or "Summarize this document.",
            "filename": filename or "document",
            "content_b64": base64.b64encode(data).decode("ascii"),
        }
        try:
            resp = httpx.post(
                self._base + "/ask-document",
                json=payload,
                headers=self._headers(),
                timeout=float(self._s.llm_timeout) + 60.0,
            )
        except httpx.HTTPError as exc:
            logger.error("kai_ask_document_failed err=%s", type(exc).__name__)
            return None, "Sorry — I couldn't process that file just now."
        if resp.status_code == 413:
            return None, "That file is too large for me to read."
        if resp.status_code != 200:
            logger.error("kai_ask_document_failed status=%d", resp.status_code)
            return None, "Sorry — I couldn't read that file."
        try:
            return resp.json(), ""
        except ValueError:
            return None, "Sorry — I got a malformed reply about that file."

    def handle_feedback(self, fb: FeedbackEvent) -> str:
        """Route a 👍/👎/escalate event to ``/feedback`` or ``/escalate``; return an ack.

        Never claims success on a FAILED call — a swallowed error must not tell the
        user a human was notified when no record was created (the never-fabricate rule
        applies to the feedback layer too)."""

        if fb.verdict == "escalate":
            data = self._post("/escalate", {"question": fb.question, "reporter": fb.sender_email})
            if data is None:
                return "Sorry — I couldn't raise that for a human just now. Please try again in a moment."
            if data.get("escalation_url"):
                return f"Raised for a human to follow up: {data['escalation_url']}"
            return "Raised for a human to follow up."
        if fb.verdict in ("up", "down"):
            data = self._post(
                "/feedback",
                {"question": fb.question, "verdict": fb.verdict, "reporter": fb.sender_email},
            )
            if data is None:
                return "Sorry — I couldn't record that just now. Please try again in a moment."
            return (
                "Thanks — noted!"
                if fb.verdict == "up"
                else "Thanks — noted. If you'd like a human to look at it, use *Escalate anyway*."
            )
        return ""

    def _post(self, path: str, payload: dict) -> dict | None:
        try:
            resp = httpx.post(
                self._base + path, json=payload, headers=self._headers(), timeout=20.0
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001 — feedback must never crash the bot
            logger.error("kai_chat_post_failed path=%s err=%s", path, type(exc).__name__)
            return None

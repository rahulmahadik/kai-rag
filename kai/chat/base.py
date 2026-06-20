"""Platform-neutral chat contracts — the seam every adapter implements.

Depends only on stdlib typing (no chat SDKs), exactly like kai.interfaces, so a
new adapter file is the ONLY thing a new platform requires.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass
class IncomingMessage:
    """A user message, normalized across platforms (bot @mention already stripped)."""

    text: str
    # An opaque per-platform sender id used for feedback attribution: a real email on
    # Webex, a username on Slack, an Azure AD object id on Teams — NOT guaranteed to
    # be an email. (Per-user access control for Slack/Teams is on the roadmap; Webex
    # has WEBEX_APPROVED_USERS/DOMAINS today.)
    sender_email: str = ""
    thread_id: str | None = None  # opaque platform thread/parent id (for threaded replies)
    raw: object = None  # escape hatch: the original SDK event


@dataclass
class FeedbackEvent:
    """A 👍/👎/escalate signal from a rich-card/button interaction."""

    verdict: str  # "up" | "down" | "escalate"
    question: str
    sender_email: str = ""
    raw: object = None


@runtime_checkable
class ChatAdapter(Protocol):
    """Transport for one chat platform.

    The adapter owns receiving and sending (and the platform's reconnect/supervision
    loop); it delegates ALL "what to answer" logic to :class:`kai.chat.service.ChatService`.
    ``run()`` blocks, servicing messages until the process is stopped.
    """

    name: str

    def run(self) -> None: ...

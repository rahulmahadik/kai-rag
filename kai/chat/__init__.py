"""Chat-platform layer for KAI.

KAI's chat surface is a thin client over the platform-agnostic HTTP API
(``POST /ask`` / ``/feedback`` / ``/escalate``). Everything platform-NEUTRAL
(calling the API, formatting the reply, feedback routing) lives in
:mod:`kai.chat.service`; everything platform-SPECIFIC (receiving/sending,
size limits, rich-card format, transport/auth) lives in a per-platform adapter
that satisfies :class:`kai.chat.base.ChatAdapter`.

Add a new platform = add one ``kai/chat/<platform>.py`` adapter; the pipeline,
the API, and the service stay untouched. Select one with ``CHAT_PLATFORM``.
"""

from __future__ import annotations


def build_chat_adapter(settings):  # noqa: ANN001 - Settings, avoid import cycle
    """Return the chat adapter selected by ``CHAT_PLATFORM`` (default ``webex``).

    Adapters are imported lazily so their SDKs (webex_bot / slack_bolt / ...) are
    only required for the platform actually in use.
    """

    platform = (getattr(settings, "chat_platform", "webex") or "webex").strip().lower()
    if platform == "webex":
        from kai.chat.webex import WebexAdapter

        return WebexAdapter(settings)
    if platform == "slack":
        from kai.chat.slack import SlackAdapter

        return SlackAdapter(settings)
    if platform == "teams":
        from kai.chat.teams import TeamsAdapter

        return TeamsAdapter(settings)
    raise ValueError(f"Unsupported CHAT_PLATFORM {platform!r}; use 'webex', 'slack', or 'teams'.")

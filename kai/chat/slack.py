"""Slack adapter, second platform proving the ChatAdapter abstraction.

Same shape as the Webex adapter: a thin client over KAI's HTTP API, all "what to
answer" logic delegated to :class:`kai.chat.service.ChatService`. Slack specifics:
Socket Mode (outbound websocket: NO public URL, like Webex), Block Kit for the
feedback buttons (Slack's Adaptive-Card equivalent), ``thread_ts`` threading, and
mrkdwn link syntax (``<url|text>``) which differs from standard markdown.

Status: COMPLETE but not live-tested here (needs a Slack workspace + the two
tokens below). Install the extra and set tokens to run:

    pip install -e '.[slack]'
    SLACK_BOT_TOKEN=xoxb-...   # Bot User OAuth token: app_mentions:read, chat:write, im:history
    SLACK_APP_TOKEN=xapp-...   # App-Level token: connections:write (Socket Mode)
    CHAT_PLATFORM=slack  python -m kai.bot

The pure helpers (``md_to_mrkdwn``, ``feedback_blocks``) are unit-tested.
"""

from __future__ import annotations

import logging
import re

from kai.chat.base import FeedbackEvent, IncomingMessage
from kai.chat.service import HELP_TEXT, ChatService, format_reply, is_help_request, split_message
from kai.config import Settings

logger = logging.getLogger("kai.chat.slack")

SLACK_TEXT_LIMIT = 2900  # Slack section text caps at 3000 chars; leave headroom
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")


def md_to_mrkdwn(text: str) -> str:
    """Convert the portable markdown from :func:`format_reply` to Slack mrkdwn.

    Slack renders links as ``<url|text>`` (not ``[text](url)``) and bold as
    ``*bold*`` (single asterisk). We translate links and ``**bold**`` → ``*bold*``;
    everything else (lists, line breaks) is already mrkdwn-compatible.
    """

    text = _MD_LINK_RE.sub(r"<\2|\1>", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"*\1*", text)
    return text


def feedback_blocks(question: str, *, escalate_only: bool = False) -> list[dict]:
    """Block Kit feedback controls.

    Confident answer → 👍 / 👎 / Escalate-anyway. Escalated answer (``escalate_only``)
    → a single **Escalate to a human** button (the answer wasn't given, so thumbs and a
    redundant "anyway" don't apply). Either way the actions carry ``block_id`` starting
    ``kai_feedback`` so :func:`collapse_feedback_blocks` retires them after one tap.
    """

    def _btn(text: str, verdict: str, style: str | None = None) -> dict:
        b = {
            "type": "button",
            "text": {"type": "plain_text", "text": text},
            "action_id": f"kai_fb_{verdict}",
            "value": question[:1900],
        }
        if style:
            b["style"] = style
        return b

    if escalate_only:
        return [
            {
                "type": "actions",
                "block_id": "kai_feedback",
                "elements": [_btn("🙋 Escalate to a human", "escalate", "primary")],
            },
        ]

    return [
        {
            "type": "context",
            "block_id": "kai_feedback_prompt",
            "elements": [{"type": "mrkdwn", "text": "Was this helpful?"}],
        },
        {
            "type": "actions",
            "block_id": "kai_feedback",
            "elements": [
                _btn("👍 Yes", "up", "primary"),
                _btn("👎 No", "down"),
                _btn("Escalate anyway", "escalate", "danger"),
            ],
        },
    ]


def collapse_feedback_blocks(blocks: list[dict], confirm: str) -> list[dict]:
    """Drop the feedback prompt + buttons and append a confirmation line.

    Tapping a button otherwise leaves the buttons live, so a user can submit 👍/👎
    repeatedly (which also skews the 👎-driven quarantine). After one tap we rewrite
    the message with the buttons gone and a short confirmation in their place.
    """
    kept = [b for b in (blocks or []) if not str(b.get("block_id", "")).startswith("kai_feedback")]
    kept.append({"type": "context", "elements": [{"type": "mrkdwn", "text": confirm}]})
    return kept


def _slack_start_hint(exc: Exception) -> str | None:
    """Translate a Slack Socket-Mode startup error into a clear fix (or None to re-raise).

    The ``missing_scope`` / ``invalid_auth`` API errors on connect are the most common
    first-run mistakes; turn them into a one-line instruction instead of a traceback.
    """
    resp = getattr(exc, "response", None)
    err = resp.get("error") if resp is not None else None
    if err == "missing_scope":
        needed = resp.get("needed") or "connections:write"
        provided = resp.get("provided") or ""
        extra = f" (it currently has '{provided}')" if provided else ""
        return (
            f"Slack rejected the Socket Mode connection: SLACK_APP_TOKEN is missing the "
            f"'{needed}' scope{extra}. App-level token scopes are fixed at creation, generate "
            "a new App-Level Token at Basic Information → App-Level Tokens with connections:write, "
            "then update SLACK_APP_TOKEN."
        )
    if err in ("invalid_auth", "not_authed", "token_revoked", "account_inactive"):
        return (
            f"Slack rejected the tokens ({err}). Re-check SLACK_BOT_TOKEN (xoxb-) and "
            "SLACK_APP_TOKEN (xapp-). They may be wrong, revoked, or from a different app."
        )
    return None


def slack_messages(
    reply_md: str, question: str, *, escalated: bool, show_buttons: bool
) -> list[dict]:
    """Build the Slack message payload(s) for one answer, PURE, unit-testable.

    Converts portable markdown → mrkdwn, splits to Slack's per-section limit, and
    attaches feedback controls to the LAST message: 👍/👎/escalate on a confident
    answer, or a single "Escalate to a human" button on an escalation.
    Returns ``[{text, blocks}, ...]``; the adapter just adds channel/thread_ts and
    calls ``say``.
    """

    pieces = split_message(md_to_mrkdwn(reply_md), SLACK_TEXT_LIMIT) or [""]
    out: list[dict] = []
    for i, piece in enumerate(pieces):
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": piece or " "}}]
        if i == len(pieces) - 1 and show_buttons:
            blocks += feedback_blocks(question, escalate_only=escalated)
        out.append({"text": piece, "blocks": blocks})
    return out


class SlackAdapter:
    """:class:`kai.chat.base.ChatAdapter` for Slack (Socket Mode)."""

    name = "slack"

    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._service = ChatService(settings)

    def run(self) -> None:
        settings, service = self._s, self._service
        bot_token = getattr(settings, "slack_bot_token", "").strip()
        app_token = getattr(settings, "slack_app_token", "").strip()
        if not bot_token or not app_token:
            raise SystemExit(
                "Slack needs SLACK_BOT_TOKEN (xoxb-...) and SLACK_APP_TOKEN (xapp-...). "
                "See kai/chat/slack.py for the required scopes."
            )
        # The two tokens are different and easily swapped (a common first-time
        # mistake): xoxb- is the Bot User OAuth token → SLACK_BOT_TOKEN; xapp- is the
        # App-Level token for Socket Mode → SLACK_APP_TOKEN. A swap otherwise fails
        # deep inside slack_bolt with a cryptic error, so check the prefixes up front.
        if not bot_token.startswith("xoxb-"):
            swap = (
                ". That is the App-Level token; put it in SLACK_APP_TOKEN instead"
                if bot_token.startswith("xapp-")
                else ""
            )
            raise SystemExit(
                "SLACK_BOT_TOKEN must start with 'xoxb-' (the Bot User OAuth Token, "
                "from OAuth & Permissions → Install to Workspace); got "
                f"'{bot_token.split('-', 1)[0]}-'{swap}."
            )
        if not app_token.startswith("xapp-"):
            swap = (
                ". That is the Bot token; put it in SLACK_BOT_TOKEN instead"
                if app_token.startswith("xoxb-")
                else ""
            )
            raise SystemExit(
                "SLACK_APP_TOKEN must start with 'xapp-' (the App-Level Token for "
                "Socket Mode, from Basic Information → App-Level Tokens); got "
                f"'{app_token.split('-', 1)[0]}-'{swap}."
            )
        try:
            from slack_bolt import App
            from slack_bolt.adapter.socket_mode import SocketModeHandler
            from slack_sdk.errors import SlackApiError
        except ModuleNotFoundError as exc:  # pragma: no cover - dep guard
            raise SystemExit("Slack support needs: pip install -e '.[slack]'") from exc

        app = App(token=bot_token)
        show_buttons = getattr(settings, "slack_feedback_buttons", True)

        def _reply(say, channel, thread_ts, text, question, escalated):  # noqa: ANN001
            pieces = split_message(md_to_mrkdwn(text), SLACK_TEXT_LIMIT)
            for i, piece in enumerate(pieces):
                blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": piece}}]
                if i == len(pieces) - 1 and show_buttons:
                    blocks += feedback_blocks(question, escalate_only=escalated)
                say(channel=channel, thread_ts=thread_ts, text=piece, blocks=blocks)

        def _handle(event, say):  # noqa: ANN001
            text = re.sub(r"<@[^>]+>", "", event.get("text", "")).strip()  # strip @mention
            if not text:
                return
            thread_ts = event.get("thread_ts") or event.get("ts")
            if is_help_request(text):
                say(channel=event["channel"], thread_ts=thread_ts, text=md_to_mrkdwn(HELP_TEXT))
                return
            data, err = service.answer(IncomingMessage(text=text, thread_id=thread_ts))
            if data is None:
                say(channel=event["channel"], thread_ts=thread_ts, text=err)
                return
            _reply(
                say, event["channel"], thread_ts, format_reply(data), text, data.get("escalated")
            )

        @app.event("app_mention")
        def _on_mention(event, say):  # noqa: ANN001
            _handle(event, say)

        @app.event("message")
        def _on_dm(event, say):  # noqa: ANN001
            if event.get("channel_type") == "im" and not event.get("bot_id"):
                _handle(event, say)

        def _register_feedback(verdict: str) -> None:
            # Bind `verdict` via a factory, NOT a default arg: slack_bolt inspects the
            # listener signature and rejects unknown params like `_v=verdict` ("not a
            # valid argument"), which silently dropped feedback. A closure is clean and
            # also captures each loop value correctly.
            @app.action(f"kai_fb_{verdict}")
            def _on_fb(ack, body, client, say):  # noqa: ANN001
                ack()
                q = body["actions"][0].get("value", "")
                actor = body.get("user", {}).get("username", "")
                msg = service.handle_feedback(
                    FeedbackEvent(verdict=verdict, question=q, sender_email=actor)
                )
                confirm = msg or "✓ Thanks for the feedback."
                message = body.get("message") or {}
                channel = (body.get("channel") or {}).get("id")
                # Remove the buttons so feedback can't be submitted twice; confirm inline.
                try:
                    client.chat_update(
                        channel=channel,
                        ts=message.get("ts"),
                        text=confirm,
                        blocks=collapse_feedback_blocks(message.get("blocks") or [], confirm),
                    )
                except Exception:  # noqa: BLE001 - fall back to a thread note
                    if msg:
                        say(channel=channel, thread_ts=message.get("thread_ts"), text=msg)

        for verdict in ("up", "down", "escalate"):
            _register_feedback(verdict)

        logger.info("kai_slack_starting api=%s", settings.kai_api_url)
        try:
            SocketModeHandler(app, app_token).start()
        except SlackApiError as exc:
            hint = _slack_start_hint(exc)
            if hint:
                raise SystemExit(hint) from exc
            raise

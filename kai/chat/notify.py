"""Proactive DM helpers for Webex, Slack, and Teams.

Used by ``POST /notify``, Inform-approve close-the-loop, and optional
space-based escalation routing (``NOTIFY_ROUTES``).
Teams needs a previously stored conversation reference (saved when the user
messages the bot); cold-open from only a user id is not supported by Bot Framework.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from kai.config import Settings

logger = logging.getLogger("kai.chat.notify")


def parse_notify_routes(raw: str) -> dict[str, str]:
    """Parse ``NOTIFY_ROUTES`` (``ENG:alice@x;OPS:U123;*:oncall@x``) → map.

    Keys are lowercased space/source tags; ``*`` is the default catch-all.
    """

    out: dict[str, str] = {}
    for part in (raw or "").split(";"):
        part = part.strip()
        if not part or ":" not in part:
            continue
        key, _, val = part.partition(":")
        key, val = key.strip().lower(), val.strip()
        if key and val:
            out[key] = val
    return out


def resolve_notify_recipient(routes: dict[str, str], spaces: Sequence[str] | None = None) -> str:
    """Pick the first route matching a space tag, else ``*``, else ``\"\"``."""

    if not routes:
        return ""
    for space in spaces or ():
        tag = (space or "").strip().lower()
        if tag and tag in routes:
            return routes[tag]
    return routes.get("*", "") or ""


def notify_on_escalation(
    settings: Settings,
    *,
    question: str,
    spaces: Sequence[str] | None = None,
    escalation_url: str | None = None,
) -> tuple[bool, str]:
    """DM the routed on-call when ``NOTIFY_ROUTES`` matches. Best-effort."""

    routes = parse_notify_routes(getattr(settings, "notify_routes", "") or "")
    who = resolve_notify_recipient(routes, spaces)
    if not who:
        return False, "no route"
    q = " ".join((question or "").split())
    if len(q) > 240:
        q = q[:237] + "..."
    msg = f"KAI escalated a question for follow-up:\n\n{q}"
    if escalation_url:
        msg += f"\n\n{escalation_url}"
    ok, detail = notify_user(settings, who, msg)
    if ok:
        logger.info("kai_escalation_notified recipient=%s", who)
    else:
        logger.warning("kai_escalation_notify_failed recipient=%s detail=%s", who, detail)
    return ok, who if ok else detail


def notify_user(settings: Settings, recipient: str, message: str) -> tuple[bool, str]:
    """Send ``message`` to ``recipient`` on the configured chat platform.

    Returns ``(ok, detail)``. ``recipient`` is an email on Webex, a Slack user id
    (``U…`` / ``W…``) or email, or a Teams AAD object id / Bot Framework user id
    that has messaged the bot before.
    """

    platform = (getattr(settings, "chat_platform", "") or "webex").strip().lower()
    recipient = (recipient or "").strip()
    message = (message or "").strip()
    if not recipient or not message:
        return False, "recipient and message are required"

    if platform == "slack":
        return _slack_dm(settings, recipient, message)
    if platform == "teams":
        from kai.chat.teams_refs import send_teams_proactive

        return send_teams_proactive(settings, recipient, message)
    token = (getattr(settings, "webex_bot_token", "") or "").strip()
    if not token:
        return False, "WEBEX_BOT_TOKEN is not configured"
    from kai.chat.webex import send_direct_message

    ok = send_direct_message(token, recipient, message)
    return (
        (True, "sent")
        if ok
        else (
            False,
            "Could not deliver (recipient may not have messaged the bot)",
        )
    )


def _slack_dm(settings: Settings, recipient: str, message: str) -> tuple[bool, str]:
    token = (getattr(settings, "slack_bot_token", "") or "").strip()
    if not token:
        return False, "SLACK_BOT_TOKEN is not configured"
    # Validate shape before importing the optional SDK so missing slack_bolt
    # does not mask a clear "bad recipient" error.
    if "@" not in recipient and not recipient.startswith(("U", "W")):
        return False, "Slack recipient must be a user id (U…) or an email"
    try:
        from slack_sdk import WebClient
        from slack_sdk.errors import SlackApiError
    except ModuleNotFoundError:
        return False, "Slack support needs: pip install -e '.[slack]'"

    client = WebClient(token=token)
    user_id = recipient
    try:
        if "@" in recipient:
            lookup = client.users_lookupByEmail(email=recipient)
            user_id = lookup["user"]["id"]
        opened = client.conversations_open(users=user_id)
        channel = (opened.get("channel") or {}).get("id")
        if not channel:
            return False, "could not open a Slack DM channel"
        client.chat_postMessage(channel=channel, text=message)
        return True, "sent"
    except SlackApiError as exc:
        err = (exc.response or {}).get("error") if exc.response is not None else type(exc).__name__
        logger.warning("kai_slack_notify_failed err=%s", err)
        return False, f"Slack API error: {err}"
    except Exception as exc:  # noqa: BLE001
        logger.warning("kai_slack_notify_failed err=%s", type(exc).__name__)
        return False, type(exc).__name__

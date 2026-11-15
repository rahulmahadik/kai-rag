"""Proactive DM helpers for Webex and Slack.

Used by ``POST /notify`` and the Inform-approve close-the-loop path.
Teams proactive DM needs a stored conversation reference and is not supported
cold; the endpoint returns a clear error for that platform.
"""

from __future__ import annotations

import logging

from kai.config import Settings

logger = logging.getLogger("kai.chat.notify")


def notify_user(settings: Settings, recipient: str, message: str) -> tuple[bool, str]:
    """Send ``message`` to ``recipient`` on the configured chat platform.

    Returns ``(ok, detail)``. ``recipient`` is an email on Webex, or a Slack
    user id (``U…`` / ``W…``) or email (needs ``users:read.email``).
    """

    platform = (getattr(settings, "chat_platform", "") or "webex").strip().lower()
    recipient = (recipient or "").strip()
    message = (message or "").strip()
    if not recipient or not message:
        return False, "recipient and message are required"

    if platform == "slack":
        return _slack_dm(settings, recipient, message)
    if platform == "teams":
        return False, (
            "Teams proactive DM needs a stored conversation reference; "
            "use Slack or Webex, or reply in-thread."
        )
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

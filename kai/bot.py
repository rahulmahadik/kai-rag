"""KAI chat bot entry point, now a thin shim over the platform-agnostic chat layer.

The bot used to be Webex-specific; it is now a launcher that builds the adapter
selected by ``CHAT_PLATFORM`` (webex | slack | ...) and runs it. All logic moved to
:mod:`kai.chat`:

* :mod:`kai.chat.service`, platform-neutral (call /ask, format reply, route feedback)
* :mod:`kai.chat.webex`, Webex adapter (websocket)
* :mod:`kai.chat.slack`, Slack adapter (Socket Mode)
* :mod:`kai.chat.teams`, Teams extension-point (documented recipe)

    Run:  python -m kai.bot           (or: python run/setup.py bot)
          CHAT_PLATFORM=slack python -m kai.bot

``format_reply`` / ``split_message`` / ``feedback_card`` are re-exported here for
backwards compatibility (existing imports and tests keep working).
"""

from __future__ import annotations

import logging

from kai.chat import build_chat_adapter
from kai.chat.service import format_reply, split_message  # noqa: F401 - back-compat re-export
from kai.chat.webex import feedback_card  # noqa: F401 - back-compat re-export
from kai.config import get_settings

logger = logging.getLogger("kai.bot")


def run_bot() -> None:
    """Build the configured chat adapter and run it (blocks)."""

    settings = get_settings()
    adapter = build_chat_adapter(settings)
    logger.info("kai_bot_platform=%s", getattr(adapter, "name", "?"))
    adapter.run()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    run_bot()

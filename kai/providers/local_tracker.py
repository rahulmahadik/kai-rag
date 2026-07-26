"""Escalation tracker fallback for when no external tracker (Jira) is wired.

Records the escalation in the logs and returns **no** ticket URL, honest
behaviour (no fake link) for a deployment that hasn't connected an issue tracker
yet. The answer pipeline shows a "flagged for a human" message without a URL in
this case. Configure Jira (JIRA_* in .env) to open real tickets instead.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("kai")


class LocalTracker:
    """A :class:`~kai.interfaces.Tracker` that logs escalations, no external API."""

    def create_issue(self, title: str, body: str) -> str:
        logger.warning("kai_escalation_unrouted title=%r", title[:160])
        return ""  # no external ticket URL

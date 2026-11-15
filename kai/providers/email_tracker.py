"""Email (SMTP) escalation tracker.

Sends the escalation to configured recipients via SMTP and returns a ``mailto:``
URL for the primary recipient (browsable contact link, not a ticket id). Same
:class:`~kai.interfaces.Tracker` contract as Jira / GitHub Issues.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage
from urllib.parse import quote

from kai.config import Settings

logger = logging.getLogger("kai.email_tracker")


def _split_addrs(raw: str) -> list[str]:
    return [a.strip() for a in (raw or "").split(",") if a.strip()]


class EmailTracker:
    """File escalations as outbound email over SMTP."""

    def __init__(self, settings: Settings) -> None:
        to = _split_addrs(getattr(settings, "escalation_email_to", "") or "")
        frm = (getattr(settings, "escalation_email_from", "") or "").strip()
        host = (getattr(settings, "escalation_smtp_host", "") or "").strip()
        if not to or not frm or not host:
            raise ValueError(
                "EmailTracker needs ESCALATION_EMAIL_TO, ESCALATION_EMAIL_FROM, "
                "and ESCALATION_SMTP_HOST in .env."
            )
        self._to = to
        self._from = frm
        self._host = host
        self._port = int(getattr(settings, "escalation_smtp_port", 0) or 0) or 587
        self._user = (getattr(settings, "escalation_smtp_user", "") or "").strip()
        self._password = getattr(settings, "escalation_smtp_password", "") or ""
        self._starttls = bool(getattr(settings, "escalation_smtp_tls", True))
        self._ssl = bool(getattr(settings, "escalation_smtp_ssl", False))

    def create_issue(self, title: str, body: str) -> str:
        """Send the escalation email; return ``mailto:`` for the primary To."""

        summary = " ".join((title or "").split()) or "KAI escalation"
        if len(summary) > 200:
            summary = summary[:197] + "..."
        msg = EmailMessage()
        msg["Subject"] = summary
        msg["From"] = self._from
        msg["To"] = ", ".join(self._to)
        msg.set_content(body or "(no body)")

        if self._ssl:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(self._host, self._port, context=context, timeout=30) as smtp:
                self._login(smtp)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(self._host, self._port, timeout=30) as smtp:
                smtp.ehlo()
                if self._starttls:
                    context = ssl.create_default_context()
                    smtp.starttls(context=context)
                    smtp.ehlo()
                self._login(smtp)
                smtp.send_message(msg)

        logger.info("kai_escalation_emailed to=%s subject=%r", self._to[0], summary[:120])
        # Contact link for the asker (not a ticket id). Subject is included so a
        # click opens a pre-filled compose window.
        return f"mailto:{self._to[0]}?subject={quote(summary)}"

    def _login(self, smtp: smtplib.SMTP) -> None:
        if self._user:
            smtp.login(self._user, self._password)

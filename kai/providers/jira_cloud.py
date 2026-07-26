"""Jira Cloud escalation tracker.

:class:`JiraCloudTracker` implements the :class:`~kai.interfaces.Tracker`
protocol by creating an issue in a Jira Cloud project via the REST v3 API and
returning the issue's browsable URL.

The issue ``description`` is sent as Atlassian Document Format (ADF), the
structured JSON body Jira Cloud's v3 ``/issue`` endpoint requires (a plain
string is rejected). We render the supplied plain-text body into ADF
paragraphs, splitting on blank lines so multi-paragraph context survives.

This module performs real network calls and is constructed by the factory at
runtime.
"""

from __future__ import annotations

import httpx

from kai.config import Settings


class JiraCloudTracker:
    """Create Jira Cloud issues for unanswerable questions (escalations).

    Authentication is HTTP Basic with ``email`` + ``api_token`` (the Atlassian
    Cloud convention). Required configuration is validated at construction so a
    blank token fails loudly before any escalation is attempted.
    """

    def __init__(self, settings: Settings) -> None:
        base_url = settings.jira_base_url.strip()
        email = settings.jira_email.strip()
        api_token = settings.jira_api_token.strip()
        project_key = settings.jira_project_key.strip()
        issue_type = settings.jira_issue_type.strip() or "Task"

        missing = [
            name
            for name, value in (
                ("jira_base_url", base_url),
                ("jira_email", email),
                ("jira_api_token", api_token),
                ("jira_project_key", project_key),
            )
            if not value
        ]
        if missing:
            raise ValueError(
                "JiraCloudTracker is missing required config: "
                + ", ".join(missing)
                + ". Set these env vars in your .env."
            )

        self._base_url = base_url.rstrip("/")
        self._project_key = project_key
        self._issue_type = issue_type
        self._auth = httpx.BasicAuth(email, api_token)
        self._timeout = httpx.Timeout(30.0)

    # ------------------------------------------------------------------
    # Tracker protocol
    # ------------------------------------------------------------------
    def create_issue(self, title: str, body: str) -> str:
        """Create an issue and return its browsable (``/browse/KEY-N``) URL."""

        payload = {
            "fields": {
                "project": {"key": self._project_key},
                "summary": self._truncate_summary(title),
                "description": self._to_adf(body),
                "issuetype": {"name": self._issue_type},
            }
        }

        endpoint = f"{self._base_url}/rest/api/3/issue"
        with httpx.Client(auth=self._auth, timeout=self._timeout) as client:
            resp = client.post(
                endpoint,
                json=payload,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
            )

        if resp.status_code not in (200, 201):
            raise RuntimeError(
                "Jira issue creation failed "
                f"(status {resp.status_code}) for project "
                f"'{self._project_key}'."
            )

        data = resp.json()
        issue_key = data.get("key")
        if not issue_key:
            raise RuntimeError("Jira issue creation returned no issue key; cannot build URL.")
        return f"{self._base_url}/browse/{issue_key}"

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    @staticmethod
    def _truncate_summary(title: str) -> str:
        """Jira summaries are capped at 255 chars and reject embedded newlines.

        Collapse ALL whitespace (incl. newlines from a pasted multi-line question)
        to single spaces, then truncate, so issue creation never 400s on format.
        """

        summary = " ".join((title or "").split()) or "KAI escalation"
        if len(summary) <= 255:
            return summary
        return summary[:252] + "..."

    @staticmethod
    def _to_adf(body: str) -> dict:
        """Render plain text into an Atlassian Document Format document.

        Splits the body on blank lines into one ADF paragraph per block. ADF
        text nodes reject empty strings, so blocks are coalesced to a single
        space when empty and the whole document always has at least one
        paragraph.
        """

        text = body or ""
        # Normalise newlines, then split into paragraph blocks on blank lines.
        normalised = text.replace("\r\n", "\n").replace("\r", "\n")
        blocks = [block.strip() for block in normalised.split("\n\n")]
        blocks = [block for block in blocks if block] or [" "]

        paragraphs = [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": block}],
            }
            for block in blocks
        ]

        return {
            "type": "doc",
            "version": 1,
            "content": paragraphs,
        }

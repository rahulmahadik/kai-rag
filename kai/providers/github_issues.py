"""GitHub Issues escalation tracker.

Creates an issue in a GitHub (or GitHub Enterprise) repository via the REST API
and returns its browsable URL. Same :class:`~kai.interfaces.Tracker` contract as
Jira, so the ask pipeline does not care which tracker is wired.
"""

from __future__ import annotations

import httpx

from kai.config import Settings


class GitHubIssuesTracker:
    """File escalations as GitHub Issues (cloud or Enterprise)."""

    def __init__(self, settings: Settings) -> None:
        token = (getattr(settings, "github_token", "") or "").strip()
        repo = (getattr(settings, "github_repo", "") or "").strip().strip("/")
        api = (getattr(settings, "github_api_url", "") or "").strip() or "https://api.github.com"
        if not token or not repo or "/" not in repo:
            raise ValueError(
                "GitHubIssuesTracker needs GITHUB_TOKEN and GITHUB_REPO (owner/name). "
                "Set these in your .env."
            )
        owner, _, name = repo.partition("/")
        if not owner or not name or "/" in name:
            raise ValueError(f"GITHUB_REPO must be 'owner/name' (got {repo!r}).")
        self._api = api.rstrip("/")
        self._repo = f"{owner}/{name}"
        self._headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        self._timeout = httpx.Timeout(30.0)

    def create_issue(self, title: str, body: str) -> str:
        """Create an issue and return its ``html_url``."""

        summary = " ".join((title or "").split()) or "KAI escalation"
        if len(summary) > 256:
            summary = summary[:253] + "..."
        endpoint = f"{self._api}/repos/{self._repo}/issues"
        with httpx.Client(timeout=self._timeout) as client:
            resp = client.post(
                endpoint,
                headers=self._headers,
                json={"title": summary, "body": body or ""},
            )
        if resp.status_code not in (200, 201):
            raise RuntimeError(
                f"GitHub issue creation failed (status {resp.status_code}) for repo '{self._repo}'."
            )
        url = (resp.json() or {}).get("html_url") or ""
        if not url:
            raise RuntimeError("GitHub issue creation returned no html_url.")
        return url

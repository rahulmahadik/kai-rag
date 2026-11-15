"""Shared allowlist checks for chat adapters.

Blank allowlists mean "anyone who can reach the bot". When either list is set,
the sender must match a listed identity (exact, case-insensitive) or, for
email-shaped identities, a listed domain.
"""

from __future__ import annotations


def _parts(raw: str) -> list[str]:
    return [p.strip().lower() for p in (raw or "").split(",") if p.strip()]


def is_allowed(
    identity: str,
    *,
    approved_users: str = "",
    approved_domains: str = "",
) -> bool:
    """Return True when ``identity`` may use the bot.

    ``approved_users`` is a comma-separated list of emails, usernames, or opaque
    platform ids. ``approved_domains`` matches the domain of an email-shaped
    identity (``user@example.com`` → ``example.com``). Both blank → allow all.
    """

    users = _parts(approved_users)
    domains = _parts(approved_domains)
    if not users and not domains:
        return True
    ident = (identity or "").strip().lower()
    if not ident:
        return False
    if ident in users:
        return True
    if "@" in ident:
        domain = ident.rsplit("@", 1)[-1]
        if domain in domains:
            return True
    return False


def deny_message() -> str:
    return "Sorry, you are not on the allowlist for this bot."

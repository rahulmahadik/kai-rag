"""Per-space answer scoping for retrieval.

Resolves which ``space`` values a caller may retrieve from, combining:

* ``ASK_SPACES`` — global default allowlist (blank = all spaces)
* ``ASK_SPACE_ACL`` — per-identity overrides (email / user id / ``@domain``)
* request ``spaces`` — optional further narrowing (never expands past the allowlist)
* ``ASK_INCLUDE_CURATED`` — when filtering, also include ``kai-curated`` (default on)

Returns ``None`` for "no filter" (full corpus) or a non-empty list for
``store.search(..., filters={"space": [...]})``. An empty list means the identity
is denied every space (retrieve nothing → escalate).
"""

from __future__ import annotations

from kai.config import Settings
from kai.pipeline.inform import CURATED_SPACE

# ACL entry separator and "identity:space,space" shape.
# Example: alice@acme.com:ENG,OPS;@acme.com:ENG;U123:HR


def _parts(raw: str) -> list[str]:
    return [p.strip() for p in (raw or "").split(",") if p.strip()]


def parse_space_acl(raw: str) -> dict[str, list[str]]:
    """Parse ``ASK_SPACE_ACL`` into ``{identity_lower: [spaces]}``."""

    out: dict[str, list[str]] = {}
    for entry in (raw or "").split(";"):
        entry = entry.strip()
        if not entry or ":" not in entry:
            continue
        ident, _, spaces = entry.partition(":")
        key = ident.strip().lower()
        vals = _parts(spaces)
        if key and vals:
            out[key] = vals
    return out


def _acl_spaces_for(identity: str, acl: dict[str, list[str]]) -> list[str] | None:
    """Return ACL spaces for ``identity``, or None when ACL does not mention them.

    Exact identity match wins; otherwise an email's ``@domain`` entry applies.
    """

    if not acl:
        return None
    ident = (identity or "").strip().lower()
    if not ident:
        # ACL is configured but no identity was supplied: deny (fail closed).
        return []
    if ident in acl:
        return list(acl[ident])
    if "@" in ident:
        domain = "@" + ident.rsplit("@", 1)[-1]
        if domain in acl:
            return list(acl[domain])
    return []  # known ACL mode, unknown user → no spaces


def resolve_spaces(
    settings: Settings,
    *,
    requested: list[str] | None = None,
    identity: str = "",
) -> list[str] | None:
    """Compute the effective space filter for this ask/search.

    ``None`` → do not filter. Non-empty list → ``filters={"space": list}``.
    Empty list → identity has no access (caller should retrieve nothing).
    """

    acl = parse_space_acl(getattr(settings, "ask_space_acl", "") or "")
    default = _parts(getattr(settings, "ask_spaces", "") or "")
    req = [s.strip() for s in (requested or []) if (s or "").strip()]

    allowed: list[str] | None
    if acl:
        allowed = _acl_spaces_for(identity, acl)
    elif default:
        allowed = list(default)
    else:
        allowed = None  # open corpus

    if allowed is None:
        effective = req or None
    elif not allowed:
        effective = []
    elif req:
        allow_set = {a.lower() for a in allowed}
        effective = [s for s in req if s.lower() in allow_set]
        # Requested spaces outside the allowlist are dropped, not errored: a chat
        # client can't expand past ACL. If nothing remains, fall back to allowed.
        if not effective:
            effective = list(allowed)
    else:
        effective = list(allowed)

    if (
        effective
        and getattr(settings, "ask_include_curated", True)
        and CURATED_SPACE.lower() not in {e.lower() for e in effective}
    ):
        effective = [*effective, CURATED_SPACE]

    return effective


def search_filters(spaces: list[str] | None) -> dict | None:
    """Translate resolved spaces into a ``VectorStore.search`` filters dict."""

    if spaces is None:
        return None
    if not spaces:
        # Sentinel the retrieve loop understands: skip search / return [].
        return {"space": []}
    return {"space": list(spaces)}

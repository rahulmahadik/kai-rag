"""Confluence Cloud knowledge-base source.

:class:`ConfluenceCloudKBSource` implements the :class:`~kai.interfaces.KBSource`
protocol by paging through the content of a single Confluence space via the
Confluence Cloud REST API and yielding one :class:`~kai.interfaces.Doc` per page.

The raw ``body.storage`` HTML (Confluence storage format) is carried through on
``Doc.html`` untouched — cleaning/chunking happens later in
``kai.pipeline.chunk`` so this module stays a thin, faithful adapter over the
remote API.

This module performs real network calls and is constructed by the factory at
runtime.
"""

from __future__ import annotations

from typing import Iterable, Iterator
from urllib.parse import urljoin, urlsplit

import httpx

from kai.config import Settings
from kai.interfaces import Doc

# Confluence Cloud caps page size at 100 for the content endpoint.
_PAGE_LIMIT = 100
# Hard ceiling on pagination loops so a misbehaving server can never hang us.
_MAX_PAGES = 10_000
# Expand the storage body, space, labels and version in one round-trip (no N+1).
_EXPAND = "body.storage,space,metadata.labels,version"


class _BearerAuth(httpx.Auth):
    """Bearer-token auth — a Confluence Server/Data Center Personal Access Token."""

    def __init__(self, token: str) -> None:
        self._token = token

    def auth_flow(self, request):  # noqa: ANN001, ANN201 — httpx auth hook
        request.headers["Authorization"] = f"Bearer {self._token}"
        yield request


class ConfluenceCloudKBSource:
    """List pages of a Confluence Cloud space as :class:`Doc` objects.

    Authentication is HTTP Basic with ``email`` + ``api_token`` (the Atlassian
    Cloud convention). All required configuration is validated up-front so a
    blank token fails loudly at construction time rather than mid-ingest.
    """

    def __init__(self, settings: Settings) -> None:
        base_url = settings.confluence_base_url.strip()
        email = settings.confluence_email.strip()
        api_token = settings.confluence_api_token.strip()
        space_key = settings.confluence_space_key.strip()

        missing = [
            name
            for name, value in (
                ("confluence_base_url", base_url),
                ("confluence_space_key", space_key),
            )
            if not value
        ]
        if missing:
            raise ValueError(
                "ConfluenceCloudKBSource is missing required config: "
                + ", ".join(missing)
                + ". Set these env vars in your .env."
            )

        # Normalise so urljoin against "/wiki/rest/..." behaves predictably.
        self._base_url = base_url.rstrip("/")
        # Namespace doc ids by host so the SAME numeric page id on two different
        # Confluence instances cannot collide (and silently overwrite) in the
        # shared vector store. Use hostname[:port] only — never userinfo — so a
        # base URL with inline credentials can't leak into stored doc ids.
        _u = urlsplit(self._base_url)
        self._host = ((_u.hostname or "") + (f":{_u.port}" if _u.port else "")) or self._base_url
        self._space_key = space_key
        # Per-instance auth (independent of every other instance):
        #   email + api_token  -> HTTP Basic (Cloud API token, or Server/DC user+pass)
        #   api_token only     -> Bearer (Server/Data Center Personal Access Token)
        #   neither            -> anonymous (public spaces)
        #   email only         -> error (an email with no token can't authenticate)
        if email and api_token:
            self._auth: httpx.Auth | None = httpx.BasicAuth(email, api_token)
        elif api_token:  # token without an email == Server/DC Personal Access Token
            self._auth = _BearerAuth(api_token)
        elif not email:  # neither set -> anonymous
            self._auth = None
        else:
            raise ValueError(
                "Confluence auth is half-configured: set confluence_api_token "
                "(with confluence_email for Cloud/Basic, or alone for a Server/DC "
                "Personal Access Token), or set NEITHER for anonymous access."
            )
        # 0 = no cap; >0 stops after this many pages (e.g. trying a big public space).
        self._max_docs = max(0, int(getattr(settings, "confluence_max_docs", 0) or 0))
        # Blank = whole space; else ingest this page + all its descendants (subtree).
        self._root_page = str(getattr(settings, "confluence_root_page", "") or "").strip()
        self._timeout = httpx.Timeout(30.0)
        # doc_ids ENCOUNTERED in the last crawl (yielded OR skipped for empty/permission),
        # so prune keeps a page that was seen-but-skipped instead of deleting it.
        self.seen_ids: set[str] = set()
        # Source-level crawl failures (for the prune partial-crawl guard); uniform with
        # CompositeKBSource so getattr(kb, "errors", 0) is meaningful for any source.
        self.errors = 0

    # ------------------------------------------------------------------
    # KBSource protocol
    # ------------------------------------------------------------------
    def iter_pages(self) -> Iterable[Doc]:
        """Yield pages as :class:`Doc` objects.

        Whole space by default; if ``confluence_root_page`` is set, only that page
        and all of its descendant pages (a subtree).
        """

        self.seen_ids = set()  # reset per crawl
        with httpx.Client(auth=self._auth, timeout=self._timeout) as client:
            if self._root_page:
                yield from self._iter_subtree(client, self._root_page)
            else:
                endpoint = f"{self._base_url}/rest/api/content"
                params = {
                    "spaceKey": self._space_key,
                    "type": "page",
                    "status": "current",
                    "expand": _EXPAND,
                }
                yield from self._iter_content(client, endpoint, params)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _iter_subtree(self, client: httpx.Client, root: str) -> Iterator[Doc]:
        """Yield a root page + all of its descendant pages (children, grandchildren …)."""

        root_id = self._resolve_page_id(client, root)
        # The root page itself.
        resp = client.get(
            f"{self._base_url}/rest/api/content/{root_id}", params={"expand": _EXPAND}
        )
        if resp.status_code == 200:
            doc = self._to_doc(resp.json())
            if doc is not None:
                yield doc
        # Every descendant page.
        endpoint = f"{self._base_url}/rest/api/content/{root_id}/descendant/page"
        yield from self._iter_content(client, endpoint, {"expand": _EXPAND})

    def _resolve_page_id(self, client: httpx.Client, root: str) -> str:
        """Resolve ``confluence_root_page`` (a page id OR exact title) to a page id."""

        root = root.strip()
        if root.isdigit():
            return root
        resp = client.get(
            f"{self._base_url}/rest/api/content",
            params={
                "spaceKey": self._space_key,
                "title": root,
                "type": "page",
                "limit": 1,
            },
        )
        if resp.status_code == 200:
            results = resp.json().get("results", [])
            if results:
                return str(results[0].get("id", "")).strip()
        raise RuntimeError(
            f"Confluence root page not found: {root!r} in space '{self._space_key}'. "
            "Set CONFLUENCE_ROOT_PAGE to an existing page id or exact title."
        )

    def _iter_content(
        self, client: httpx.Client, endpoint: str, base_params: dict
    ) -> Iterator[Doc]:
        """Paginate a Confluence content listing, yielding one :class:`Doc` per page.

        Generic over the endpoint/params so it serves both the whole-space listing
        and the per-subtree descendant listing. Honours ``confluence_max_docs``.
        """

        start = 0
        yielded = 0
        for _ in range(_MAX_PAGES):
            params = {**base_params, "start": start, "limit": _PAGE_LIMIT}
            resp = client.get(endpoint, params=params)
            if resp.status_code != 200:
                raise RuntimeError(
                    f"Confluence content request failed (status {resp.status_code}) "
                    f"at {endpoint} (start={start})."
                )

            payload = resp.json()
            results = payload.get("results", [])
            for result in results:
                doc = self._to_doc(result)
                if doc is not None:
                    yield doc
                    yielded += 1
                    if self._max_docs and yielded >= self._max_docs:
                        return

            # Absence of _links.next (or a short page) means we've reached the end.
            next_link = payload.get("_links", {}).get("next")
            if not next_link or len(results) < _PAGE_LIMIT:
                return
            start += _PAGE_LIMIT
        raise RuntimeError(
            f"Confluence pagination exceeded {_MAX_PAGES} pages at {endpoint}; "
            "aborting to avoid an infinite loop."
        )

    def _to_doc(self, result: dict) -> Doc | None:
        """Map one Confluence content object to a :class:`Doc`.

        Returns ``None`` for results that carry no storage body (e.g. pages the
        caller lacks permission to read) so they are skipped rather than ingested
        as empty documents.
        """

        page_id = str(result.get("id", "")).strip()
        if not page_id:
            return None
        doc_id = f"{self._host}:{page_id}"
        # Mark the page SEEN before any skip below, so prune won't delete a page that
        # exists upstream but was transiently skipped (no body / permission blip).
        self.seen_ids.add(doc_id)

        html = result.get("body", {}).get("storage", {}).get("value", "")
        if not html:
            return None

        title = result.get("title", "") or page_id
        space = result.get("space", {}).get("key", "") or self._space_key

        labels = [
            label.get("name", "")
            for label in result.get("metadata", {}).get("labels", {}).get("results", [])
            if label.get("name")
        ]

        updated = result.get("version", {}).get("when", "") if result.get("version") else ""

        url = self._build_web_url(result, page_id)

        return Doc(
            id=doc_id,
            title=title,
            url=url,
            html=html,
            space=space,
            labels=labels,
            updated=updated,
        )

    def _build_web_url(self, result: dict, page_id: str) -> str:
        """Resolve the human-browsable URL for a page.

        Prefers the ``_links.webui`` relative path returned by the API (resolved
        against ``_links.base``), falling back to a tiny-link style URL built
        from the configured base.
        """

        links = result.get("_links", {})
        webui = links.get("webui", "")
        if webui:
            # ``base`` is the wiki root, e.g. https://acme.atlassian.net/wiki
            link_base = links.get("base", "") or self._base_url
            return urljoin(link_base.rstrip("/") + "/", webui.lstrip("/"))
        # Stable fallback: the pages REST viewer accepts the raw page id.
        return f"{self._base_url}/pages/viewpage.action?pageId={page_id}"

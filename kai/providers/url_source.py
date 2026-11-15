"""URL / sitemap knowledge-base source.

Fetches one or more seed URLs (and optionally URLs listed in a sitemap.xml),
extracts readable text from HTML, and yields :class:`~kai.interfaces.Doc`
values. Same chunk → embed → retrieve path as Confluence and files.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Iterator
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx

from kai.config import Settings
from kai.interfaces import Doc

logger = logging.getLogger("kai.urls")

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


class _TextExtractor(HTMLParser):
    """Collect visible text; skip script/style/noscript."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip = 0
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        t = tag.lower()
        if t in {"script", "style", "noscript", "svg"}:
            self._skip += 1
        if t == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in {"script", "style", "noscript", "svg"} and self._skip:
            self._skip -= 1
        if t == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        text = data.strip()
        if not text:
            return
        if self._in_title and not self.title:
            self.title = text
        self._chunks.append(text)

    def text(self) -> str:
        return _WS_RE.sub(" ", " ".join(self._chunks)).strip()


def _html_to_text(html: str) -> tuple[str, str]:
    parser = _TextExtractor()
    try:
        parser.feed(html or "")
        parser.close()
    except Exception:  # noqa: BLE001 - best-effort extract
        plain = _WS_RE.sub(" ", _TAG_RE.sub(" ", html or "")).strip()
        return "", plain
    return parser.title, parser.text()


def _doc_id_for(url: str) -> str:
    """Stable id from the URL host+path (scheme/query dropped)."""

    p = urlparse(url)
    host = (p.netloc or "url").lower()
    path = (p.path or "/").rstrip("/") or "/"
    return f"url:{host}{path}"


def _parse_sitemap(body: str, base: str) -> list[str]:
    urls: list[str] = []
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return urls
    # Sitemap index or urlset; ignore namespaces by local-name.
    for el in root.iter():
        tag = el.tag.rsplit("}", 1)[-1].lower()
        if tag == "loc" and (el.text or "").strip():
            urls.append(urljoin(base, el.text.strip()))
    return urls


class UrlKBSource:
    """Crawl configured seed URLs and/or a sitemap into Docs."""

    def __init__(self, settings: Settings) -> None:
        seeds = [
            u.strip() for u in (getattr(settings, "source_urls", "") or "").split(",") if u.strip()
        ]
        sitemap = (getattr(settings, "source_sitemap", "") or "").strip()
        if not seeds and not sitemap:
            raise ValueError(
                "URL knowledge source needs SOURCE_URLS and/or SOURCE_SITEMAP in .env."
            )
        self._seeds = seeds
        self._sitemap = sitemap
        self._max = int(getattr(settings, "source_url_max_pages", 0) or 0) or 100
        self._timeout = httpx.Timeout(30.0)
        self._headers = {
            "User-Agent": "KAI-url-source/1.2 (+https://github.com/rahulmahadik/kai-rag)"
        }

    def _fetch(self, client: httpx.Client, url: str) -> tuple[str, str] | None:
        try:
            resp = client.get(url, follow_redirects=True)
        except httpx.HTTPError as exc:
            logger.warning("kai_url_fetch_failed url=%s err=%s", url, type(exc).__name__)
            return None
        if resp.status_code != 200:
            logger.warning("kai_url_fetch_status url=%s status=%d", url, resp.status_code)
            return None
        ctype = (resp.headers.get("content-type") or "").lower()
        text = resp.text or ""
        head = text.lstrip().lower()
        looks_html = (
            "html" in ctype or head.startswith("<!doctype html") or "<html" in text[:500].lower()
        )
        if looks_html:
            title, body = _html_to_text(text)
            return title or url, body
        if "xml" in ctype or url.rstrip("/").endswith(".xml"):
            return "", text
        # Plain text / markdown endpoints.
        return url, text.strip()

    def iter_pages(self) -> Iterable[Doc]:
        seen: set[str] = set()
        queue: list[str] = list(self._seeds)
        yielded = 0
        with httpx.Client(timeout=self._timeout, headers=self._headers) as client:
            if self._sitemap:
                sm = self._fetch(client, self._sitemap)
                if sm is not None:
                    for u in _parse_sitemap(sm[1], self._sitemap):
                        if u not in queue:
                            queue.append(u)
            for url in queue:
                if yielded >= self._max:
                    break
                if url in seen:
                    continue
                seen.add(url)
                got = self._fetch(client, url)
                if got is None:
                    continue
                title, body = got
                is_sitemap_body = body.lstrip().startswith("<?xml") and "<urlset" in body
                if url == self._sitemap or is_sitemap_body:
                    # Sitemap body itself is not a knowledge page.
                    continue
                if not body or len(body) < 40:
                    logger.info("kai_url_skip_empty url=%s", url)
                    continue
                yielded += 1
                yield Doc(
                    id=_doc_id_for(url),
                    title=title or url,
                    url=url,
                    html=body,
                    space="url",
                    content_type="text",
                )


def iter_url_docs(settings: Settings) -> Iterator[Doc]:
    """Convenience for tests."""

    yield from UrlKBSource(settings).iter_pages()

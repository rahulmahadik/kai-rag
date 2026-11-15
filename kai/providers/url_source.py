"""URL / sitemap knowledge-base source.

Fetches seed URLs and/or a sitemap.xml, optionally follows same-host links,
extracts readable text from HTML, and yields :class:`~kai.interfaces.Doc`
values. Same chunk → embed → retrieve path as Confluence and files.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from collections import deque
from collections.abc import Iterable, Iterator
from html.parser import HTMLParser
from urllib.parse import urldefrag, urljoin, urlparse

import httpx

from kai.config import Settings
from kai.interfaces import Doc

logger = logging.getLogger("kai.urls")

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_SKIP_SUFFIXES = (
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".webp",
    ".ico",
    ".css",
    ".js",
    ".mjs",
    ".map",
    ".woff",
    ".woff2",
    ".ttf",
    ".zip",
    ".gz",
    ".tgz",
    ".pdf",  # PDF needs a different extractor; skip in the HTML crawler
)


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


class _LinkExtractor(HTMLParser):
    """Collect ``href`` targets from ``<a>`` tags."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if tag.lower() != "a":
            return
        for k, v in attrs:
            if k.lower() == "href" and v:
                self.hrefs.append(v)
                return


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


def _normalize_url(url: str) -> str:
    """Drop fragment; trim trailing slash on non-root paths for de-dupe."""

    clean, _frag = urldefrag((url or "").strip())
    if not clean:
        return ""
    p = urlparse(clean)
    if p.scheme not in {"http", "https"}:
        return ""
    path = p.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    return p._replace(path=path, fragment="").geturl()


def same_host_links(base_url: str, html: str) -> list[str]:
    """Return absolute same-host http(s) links found in ``html``."""

    base = _normalize_url(base_url)
    if not base:
        return []
    host = urlparse(base).netloc.lower()
    parser = _LinkExtractor()
    try:
        parser.feed(html or "")
        parser.close()
    except Exception:  # noqa: BLE001
        return []
    out: list[str] = []
    seen: set[str] = set()
    for href in parser.hrefs:
        abs_url = _normalize_url(urljoin(base, href))
        if not abs_url or abs_url in seen:
            continue
        p = urlparse(abs_url)
        if p.netloc.lower() != host:
            continue
        if any(p.path.lower().endswith(suf) for suf in _SKIP_SUFFIXES):
            continue
        seen.add(abs_url)
        out.append(abs_url)
    return out


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
        self._follow = bool(getattr(settings, "source_url_follow_links", True))
        self._timeout = httpx.Timeout(30.0)
        self._headers = {
            "User-Agent": "KAI-url-source/1.2 (+https://github.com/rahulmahadik/kai-rag)"
        }

    def _fetch(self, client: httpx.Client, url: str) -> tuple[str, str, str] | None:
        """Return ``(title, body_text, raw)`` or None."""

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
            return title or url, body, text
        if "xml" in ctype or url.rstrip("/").endswith(".xml"):
            return "", text, text
        return url, text.strip(), text

    def iter_pages(self) -> Iterable[Doc]:
        seen: set[str] = set()
        queue: deque[str] = deque(_normalize_url(u) or u for u in self._seeds)
        yielded = 0
        with httpx.Client(timeout=self._timeout, headers=self._headers) as client:
            if self._sitemap:
                sm = self._fetch(client, self._sitemap)
                if sm is not None:
                    for u in _parse_sitemap(sm[1], self._sitemap):
                        nu = _normalize_url(u) or u
                        if nu not in queue:
                            queue.append(nu)
            while queue:
                if yielded >= self._max:
                    break
                url = queue.popleft()
                if not url or url in seen:
                    continue
                seen.add(url)
                got = self._fetch(client, url)
                if got is None:
                    continue
                title, body, raw = got
                is_sitemap_body = body.lstrip().startswith("<?xml") and "<urlset" in body
                if url == self._sitemap or is_sitemap_body:
                    # Sitemap body itself is not a knowledge page; still enqueue locs.
                    if is_sitemap_body:
                        for u in _parse_sitemap(body, url):
                            nu = _normalize_url(u) or u
                            if nu and nu not in seen:
                                queue.append(nu)
                    continue
                if not body or len(body) < 40:
                    logger.info("kai_url_skip_empty url=%s", url)
                    continue
                if self._follow:
                    for link in same_host_links(url, raw):
                        if link not in seen:
                            queue.append(link)
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

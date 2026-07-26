"""The web chat UI, driven in a real browser against a real API.

`frontend/index.html` is a standalone page with no build step, so nothing else in
the test tree executes its JavaScript. These cases load the actual file in Chromium,
point it at a live KAI API, and assert on the rendered DOM: what an answer looks
like, what an escalation looks like, and that nothing in a model answer or a
citation can become executable markup.

Skipped unless Playwright and its Chromium build are installed:

    pip install playwright && playwright install chromium
"""

from __future__ import annotations

import json
import os
import threading
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

import pytest

# A silent skip would leave the frontend's escaping and URL-scheme guards
# unverified while the run stayed green, so the job that OWNS these tests sets
# KAI_REQUIRE_UI_TESTS and gets a hard failure if the browser is missing.
# Keying this off CI instead would break every other CI job, since GitHub sets
# CI=true everywhere but only the integration job installs Playwright.
_REQUIRE_UI = bool(os.environ.get("KAI_REQUIRE_UI_TESTS"))
if not _REQUIRE_UI:
    pytest.importorskip("playwright.sync_api", reason="pip install playwright")
from playwright.sync_api import sync_playwright  # noqa: E402

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"

# One canned reply per question, so a case can ask for the exact shape it needs.
REPLIES: dict[str, dict] = {
    "answered": {
        "answer": "Replication copies data between brokers [1].",
        "citations": [{"title": "Replication guide", "url": "https://kb.example/replication"}],
        "confidence": 0.91,
        "escalated": False,
        "suggested_sources": [],
    },
    "escalated": {
        "answer": "I couldn't answer this confidently from the knowledge base.",
        "citations": [],
        "confidence": 0.21,
        "escalated": True,
        "suggested_sources": [{"title": "Closest page", "url": "https://kb.example/close"}],
    },
    "xss": {
        "answer": '<img src=x onerror="window.__pwned=1"> and <script>window.__pwned=1</script>',
        "citations": [],
        "confidence": 0.9,
        "escalated": False,
        "suggested_sources": [],
    },
    "badscheme": {
        "answer": "See the guide [1].",
        "citations": [
            {"title": "Evil", "url": "javascript:window.__pwned=1"},
            {"title": "Good", "url": "https://kb.example/ok"},
        ],
        "confidence": 0.9,
        "escalated": False,
        "suggested_sources": [],
    },
    "markdown": {
        "answer": "Use **bold**, `code`, and a [link](https://kb.example/deep).\nSecond line.",
        "citations": [],
        "confidence": 0.8,
        "escalated": False,
        "suggested_sources": [],
    },
}


class _StubAPI(SimpleHTTPRequestHandler):
    """A stand-in KAI API: CORS preflight, /ask and /ask-document."""

    def log_message(self, *a) -> None:  # keep pytest output clean
        pass

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        key = (body.get("question") or "").strip()
        if self.path == "/ask-document":
            payload = {**REPLIES["answered"], "answer": f"About {body.get('filename')} [1]."}
        elif key == "boom":
            self.send_response(500)
            self._cors()
            self.end_headers()
            return
        else:
            payload = REPLIES.get(key, REPLIES["answered"])
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self._cors()
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture(scope="module")
def api_url():
    server = HTTPServer(("127.0.0.1", 0), _StubAPI)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


@pytest.fixture(scope="module")
def ui_url():
    handler = type("_Static", (SimpleHTTPRequestHandler,), {"log_message": lambda *a: None})
    server = HTTPServer(("127.0.0.1", 0), lambda *a, **k: handler(*a, directory=str(FRONTEND), **k))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/index.html"
    server.shutdown()


@pytest.fixture(scope="module")
def browser():
    try:
        with sync_playwright() as pw:
            b = pw.chromium.launch()
            yield b
            b.close()
    except Exception as exc:  # noqa: BLE001 - the browser build may be absent
        if _REQUIRE_UI:
            raise  # this job is meant to have a browser; a missing one is a break
        pytest.skip(f"chromium unavailable: {type(exc).__name__}: {exc}")


@pytest.fixture
def page(browser, ui_url, api_url):
    """A loaded page already pointed at the stub API, with console errors captured."""

    ctx = browser.new_context()
    p = ctx.new_page()
    errors: list[str] = []
    p.on("pageerror", lambda e: errors.append(str(e)))
    p.goto(ui_url)
    p.fill("#api", api_url)
    p.dispatch_event("#api", "change")
    p.errors = errors  # type: ignore[attr-defined]
    yield p
    ctx.close()


def _wait_for_reply(page) -> None:
    """Wait until the newest KAI bubble has stopped showing the searching animation.

    The opening greeting is plain `.row`, so only replies carry `.row.kai`.
    """

    page.wait_for_function(
        "() => { const b = document.querySelectorAll('.row.kai .bubble');"
        " return b.length > 0 && !b[b.length-1].querySelector('.dots'); }",
        timeout=15000,
    )


def _ask(page, question: str) -> str:
    """Ask, wait for the reply bubble, and return its text."""

    page.fill("#q", question)
    page.click("#send")
    _wait_for_reply(page)
    return page.locator(".row.kai .bubble").last.inner_text()


# ======================================================================= #
# The page itself
# ======================================================================= #
def test_the_page_loads_without_a_javascript_error(page) -> None:
    assert page.title()
    assert page.locator("#q").is_visible()
    assert page.locator("#send").is_visible()
    assert page.errors == []


def test_the_api_base_is_remembered_across_reloads(page, ui_url, api_url) -> None:
    page.reload()
    assert page.input_value("#api") == api_url


# ======================================================================= #
# Answering
# ======================================================================= #
def test_a_confident_answer_renders_with_its_sources(page) -> None:
    text = _ask(page, "answered")

    assert "Replication copies data between brokers" in text
    bubble = page.locator(".row.kai .bubble").last
    assert "Sources" in bubble.inner_text()
    assert bubble.locator(".pill.ok").inner_text() == "answered"
    assert "confidence 0.91" in bubble.locator(".meta").first.inner_text()
    link = bubble.locator(".sources a").first
    assert link.get_attribute("href") == "https://kb.example/replication"
    assert link.inner_text() == "Replication guide"


def test_the_question_is_echoed_in_a_user_bubble(page) -> None:
    _ask(page, "answered")
    assert page.locator(".row.me .bubble").last.inner_text() == "answered"


def test_an_escalation_is_visually_distinct_and_cites_nothing(page) -> None:
    _ask(page, "escalated")

    bubble = page.locator(".row.kai .bubble").last
    assert "escalated" in (bubble.get_attribute("class") or "")
    assert bubble.locator(".pill.esc").inner_text() == "escalated"
    assert "Closest pages (not a confirmed answer)" in bubble.inner_text()
    # The retrieval score must not be shown as answer confidence on an escalation.
    assert "confidence" not in bubble.locator(".meta").first.inner_text()


def test_markdown_is_rendered_rather_than_shown_raw(page) -> None:
    _ask(page, "markdown")

    bubble = page.locator(".row.kai .bubble").last
    assert bubble.locator("b").first.inner_text() == "bold"
    assert bubble.locator("code").first.inner_text() == "code"
    link = bubble.locator("a", has_text="link").first
    assert link.get_attribute("href") == "https://kb.example/deep"
    assert link.get_attribute("rel") == "noopener noreferrer"


def test_a_server_error_is_reported_and_the_input_recovers(page) -> None:
    text = _ask(page, "boom")

    assert "something went wrong (500)" in text.lower()
    assert page.locator("#send").is_enabled(), "the composer must not stay disabled"


# ======================================================================= #
# Injection: an answer and a citation are untrusted content
# ======================================================================= #
def test_html_in_an_answer_is_escaped_not_executed(page) -> None:
    text = _ask(page, "xss")

    assert page.evaluate("() => window.__pwned") is None, "answer markup executed"
    bubble = page.locator(".row.kai .bubble").last
    assert bubble.locator("img").count() == 0
    assert bubble.locator("script").count() == 0
    assert "<img" in text, "the markup should be visible as text"


def test_a_javascript_citation_url_never_becomes_a_link(page) -> None:
    """Citation URLs come from the corpus, so they are only as trustworthy as
    whoever can write to it."""

    _ask(page, "badscheme")

    bubble = page.locator(".row.kai .bubble").last
    hrefs = [
        bubble.locator(".sources a").nth(i).get_attribute("href")
        for i in range(bubble.locator(".sources a").count())
    ]
    assert hrefs == ["https://kb.example/ok"], f"unsafe scheme rendered: {hrefs}"
    assert page.evaluate("() => window.__pwned") is None


# ======================================================================= #
# File attachment
# ======================================================================= #
def test_attaching_a_file_shows_a_pill_and_scopes_the_question(page, tmp_path) -> None:
    doc = tmp_path / "notes.txt"
    doc.write_text("some content")
    page.set_input_files("#file", str(doc))

    assert page.locator("#filebar").is_visible()
    assert "notes.txt" in page.locator(".filepill").inner_text()
    assert "not saved" in page.locator("#filebar").inner_text()

    page.fill("#q", "what is this")
    page.click("#send")
    _wait_for_reply(page)

    bubble = page.locator(".row.kai .bubble").last
    assert "About notes.txt" in bubble.inner_text()
    assert "isn't saved to the knowledge base" in bubble.inner_text()
    assert not page.locator("#filebar").is_visible(), "the pill must clear after sending"


def test_removing_an_attachment_clears_it(page, tmp_path) -> None:
    doc = tmp_path / "notes.txt"
    doc.write_text("x")
    page.set_input_files("#file", str(doc))
    page.click(".filepill .x")

    assert not page.locator("#filebar").is_visible()
    assert page.get_attribute("#q", "placeholder") == "Ask a question..."


def test_an_example_chip_submits_its_question(page) -> None:
    page.locator(".chip").first.click()
    _wait_for_reply(page)
    assert page.locator(".row.me .bubble").count() == 1

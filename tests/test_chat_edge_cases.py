"""Exhaustive edge-case coverage for every platform's REPLY path.

Multi-persona: a tester hunting for broken replies, an integration engineer for
malformed/partial API responses, a UX reviewer for empty/escalated/long answers.
The transport (SDK send/receive) needs a live bot; everything that SHAPES the
reply is pure and covered here.
"""

from __future__ import annotations

import httpx
import pytest

from kai.chat.base import FeedbackEvent, IncomingMessage
from kai.chat.service import HELP_TEXT, ChatService, format_reply, is_help_request, split_message
from kai.chat.slack import feedback_blocks, md_to_mrkdwn, slack_messages
from kai.chat.webex import (
    create_message,
    edit_message,
    feedback_card,
    send_direct_message,
    webex_reply,
)
from kai.config import Settings


def _s(**o):
    return Settings(_env_file=None, kai_api_url="http://x", llm_timeout=1, **o)


# ======================================================================= #
# format_reply: the shared answer content (every shape /ask can return)
# ======================================================================= #
@pytest.mark.parametrize(
    "payload,must,mustnt",
    [
        ({"answer": "", "citations": [], "escalated": False}, ["don't have an answer"], []),
        ({"answer": "   ", "escalated": False}, ["don't have an answer"], []),
        (
            {
                "answer": "A [1].",
                "citations": [{"title": "T", "url": "http://u"}],
                "escalated": False,
            },
            ["A [1].", "**Sources:**", "[T](http://u)"],
            [],
        ),
        ({"answer": "A.", "citations": [], "escalated": False}, ["A."], ["Sources"]),
        (
            {"answer": "A.", "citations": [{"title": "only-title", "url": ""}], "escalated": False},
            ["A."],
            ["Sources"],
        ),  # citation with no URL is skipped → no Sources block
        (
            {
                "answer": "Esc.",
                "escalated": True,
                "suggested_sources": [{"title": "S", "url": "http://s"}],
            },
            ["Esc.", "Closest pages", "[S](http://s)"],
            ["Sources:"],
        ),
        ({"answer": "Esc.", "escalated": True, "suggested_sources": []}, ["Esc."], ["Closest"]),
        (
            {
                "answer": "A [1][2].",
                "citations": [
                    {"title": "A", "url": "http://1"},
                    {"title": "A2", "url": "http://1"},
                    {"title": "B", "url": "http://2"},
                ],
                "escalated": False,
            },
            ["http://1", "http://2"],
            [],
        ),  # dedup by URL
        ({"answer": "A."}, ["A."], []),  # missing 'citations'/'escalated' keys → graceful
    ],
)
def test_format_reply_shapes(payload, must, mustnt):
    out = format_reply(payload)
    for m in must:
        assert m in out, (m, out)
    for m in mustnt:
        assert m not in out, (m, out)


def test_format_reply_dedup_count():
    out = format_reply(
        {
            "answer": "X.",
            "escalated": False,
            "citations": [{"title": "A", "url": "http://1"}, {"title": "A2", "url": "http://1"}],
        }
    )
    assert out.count("http://1") == 1


# ======================================================================= #
# split_message, sizing across boundaries + unicode
# ======================================================================= #
def test_split_empty_and_short():
    assert split_message("", 100) == []
    assert split_message("hi", 100) == ["hi"]


def test_split_exactly_at_limit():
    t = "x" * 100
    assert split_message(t, 100) == [t]


def test_split_long_paragraphs_and_all_text_kept():
    t = "\n\n".join(f"P{i} " + "w " * 40 for i in range(15))
    pieces = split_message(t, 500)
    assert len(pieces) > 1
    assert all(len(p.encode()) <= 500 for p in pieces)
    assert "P0" in pieces[0] and "P14" in pieces[-1]


def test_split_single_long_line_hard_cut():
    t = "word " * 500  # no paragraph breaks
    pieces = split_message(t, 300)
    assert all(len(p.encode()) <= 300 for p in pieces)
    assert "".join(p.replace(" ", "") for p in pieces).count("word") == 500


def test_split_unicode_is_byte_safe():
    t = "café 🚀  résumé " * 60
    pieces = split_message(t, 200)
    assert all(len(p.encode()) <= 200 for p in pieces)
    for p in pieces:
        p.encode("utf-8")  # must not raise / contain broken surrogates


# ======================================================================= #
# Slack rendering, mrkdwn + Block Kit
# ======================================================================= #
@pytest.mark.parametrize(
    "md,expect",
    [
        ("[T](http://u)", "<http://u|T>"),
        ("**bold**", "*bold*"),
        ("see [A](http://a) and [B](http://b)", "<http://a|A>"),
        ("plain text no links", "plain text no links"),
    ],
)
def test_md_to_mrkdwn(md, expect):
    assert expect in md_to_mrkdwn(md)


def test_slack_messages_confident_has_buttons_on_last_only():
    long = "\n\n".join(f"para {i} " + "word " * 80 for i in range(20))  # > 2900 bytes
    msgs = slack_messages(long, "q", escalated=False, show_buttons=True)
    assert len(msgs) > 1
    assert not any(b["type"] == "actions" for b in msgs[0]["blocks"])
    assert any(b["type"] == "actions" for b in msgs[-1]["blocks"])


def test_slack_messages_escalated_shows_escalate_only_button():
    # Escalations now carry a single "Escalate to a human" button (no 👍/👎).
    msgs = slack_messages("Esc.", "q", escalated=True, show_buttons=True)
    actions = [b for m in msgs for b in m["blocks"] if b["type"] == "actions"]
    assert len(actions) == 1
    assert {e["action_id"] for e in actions[0]["elements"]} == {"kai_fb_escalate"}


def test_slack_messages_buttons_disabled():
    msgs = slack_messages("A.", "q", escalated=False, show_buttons=False)
    assert not any(b["type"] == "actions" for m in msgs for b in m["blocks"])


def test_slack_never_emits_empty_text_block():
    # Slack rejects an empty section text; we substitute a space.
    msgs = slack_messages("", "q", escalated=False, show_buttons=False)
    assert msgs and msgs[0]["blocks"][0]["text"]["text"] == " "


def test_slack_feedback_blocks_action_ids():
    actions = next(b for b in feedback_blocks("q") if b["type"] == "actions")["elements"]
    assert {a["action_id"] for a in actions} == {"kai_fb_up", "kai_fb_down", "kai_fb_escalate"}


# ======================================================================= #
# Webex rendering, pieces + Adaptive Card
# ======================================================================= #
def test_webex_reply_card_confident_vs_escalation():
    _p, c = webex_reply({"answer": "A.", "escalated": False}, "q", show_card=True)
    assert c is not None and c["type"] == "AdaptiveCard"
    assert {a["data"]["verdict"] for a in c["actions"]} == {"up", "down", "escalate"}
    _p, c = webex_reply({"answer": "Esc.", "escalated": True}, "q", show_card=True)
    # escalation → a single "Escalate to a human" card (not None)
    assert c is not None
    assert {a["data"]["verdict"] for a in c["actions"]} == {"escalate"}


def test_webex_reply_card_disabled():
    _, c = webex_reply({"answer": "A.", "escalated": False}, "q", show_card=False)
    assert c is None


def test_webex_reply_always_one_piece():
    p, _ = webex_reply({"answer": "", "escalated": False}, "q", show_card=False)
    assert len(p) >= 1


def test_webex_card_verdicts():
    assert {a["data"]["verdict"] for a in feedback_card("q")["actions"]} == {
        "up",
        "down",
        "escalate",
    }


# ======================================================================= #
# ChatService, every HTTP outcome maps to a sane reply, never raises
# ======================================================================= #
class _R:
    def __init__(self, status, payload=None, bad_json=False):
        self.status_code = status
        self._p, self._bad = payload or {}, bad_json

    def json(self):
        if self._bad:
            raise ValueError("bad json")
        return self._p

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("e", request=None, response=None)


@pytest.mark.parametrize(
    "status,expect_none,needle",
    [
        (200, False, None),
        (401, True, "administrator"),
        (403, True, "administrator"),
        (422, True, "rephrasing"),
        (500, True, "problem"),
    ],
)
def test_service_answer_status_paths(monkeypatch, status, expect_none, needle):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _R(status, {"answer": "ok"}))
    data, err = ChatService(_s()).answer(IncomingMessage(text="q"))
    assert (data is None) is expect_none
    if needle:
        assert needle in err


def test_service_answer_network_and_timeout(monkeypatch):
    for exc in (httpx.TimeoutException("t"), httpx.ConnectError("c")):

        def _boom(*a, _e=exc, **k):
            raise _e

        monkeypatch.setattr(httpx, "post", _boom)
        data, err = ChatService(_s()).answer(IncomingMessage(text="q"))
        assert data is None and err


def test_service_answer_bad_json(monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _R(200, bad_json=True))
    data, err = ChatService(_s()).answer(IncomingMessage(text="q"))
    assert data is None and "malformed" in err.lower()


@pytest.mark.parametrize(
    "verdict,endpoint,needle",
    [
        ("up", "/feedback", "noted"),
        ("down", "/feedback", "noted"),
        ("escalate", "/escalate", "follow up"),
    ],
)
def test_service_feedback_routes(monkeypatch, verdict, endpoint, needle):
    seen = {}
    monkeypatch.setattr(
        httpx,
        "post",
        lambda url, **k: (seen.__setitem__("u", url), _R(200, {"escalation_url": "http://t"}))[1],
    )
    msg = ChatService(_s()).handle_feedback(FeedbackEvent(verdict=verdict, question="q"))
    assert seen["u"].endswith(endpoint) and needle in msg.lower()


@pytest.mark.parametrize(
    "text,expect",
    [
        ("help", True),
        ("Help", True),
        ("help?", True),
        ("/help", True),
        ("?", True),
        ("what can you do?", True),
        ("commands", True),
        ("How does this work?", True),
        ("how do I reset my password", False),
        ("help me reset the database", False),
        ("helping hand", False),
        ("", False),
    ],
)
def test_is_help_request(text, expect):
    assert is_help_request(text) is expect


def test_help_text_advertises_key_capabilities():
    # The one place users learn what KAI can do, must name asking, file upload,
    # and escalation so "help" is genuinely discoverable.
    assert "Attach a file" in HELP_TEXT
    assert "escalate" in HELP_TEXT.lower()
    assert "isn't saved" in HELP_TEXT  # sets the one-off expectation up front


def test_service_feedback_unknown_verdict_is_noop():
    assert ChatService(_s()).handle_feedback(FeedbackEvent(verdict="huh", question="q")) == ""


def test_service_feedback_survives_post_failure(monkeypatch):
    def _boom(*a, **k):
        raise httpx.HTTPError("down")

    monkeypatch.setattr(httpx, "post", _boom)
    svc = ChatService(_s())
    # A dead endpoint must NOT claim success: never-fabricate at the feedback layer:
    # it returns a clear "couldn't" message (and never raises).
    esc = svc.handle_feedback(FeedbackEvent(verdict="escalate", question="q")).lower()
    assert "couldn't" in esc and "follow up" not in esc
    down = svc.handle_feedback(FeedbackEvent(verdict="down", question="q")).lower()
    assert "couldn't" in down and "noted" not in down


# ======================================================================= #
# Webex REST helpers, success / API-error / exception (never raise)
# ======================================================================= #
def test_webex_rest_success(monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _R(200, {"id": "M1"}))
    monkeypatch.setattr(httpx, "put", lambda *a, **k: _R(200))
    assert create_message("t", roomId="R", markdown="x") == "M1"
    assert edit_message("t", "M1", "R", "x") is True
    assert send_direct_message("t", "a@b.c", "x") is True


def test_webex_rest_api_error(monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _R(429))
    monkeypatch.setattr(httpx, "put", lambda *a, **k: _R(400))
    assert create_message("t", roomId="R", markdown="x") is None
    assert edit_message("t", "M", "R", "x") is False


def test_webex_rest_exception(monkeypatch):
    def _boom(*a, **k):
        raise httpx.ConnectError("x")

    monkeypatch.setattr(httpx, "post", _boom)
    monkeypatch.setattr(httpx, "put", _boom)
    assert create_message("t", roomId="R", markdown="x") is None
    assert edit_message("t", "M", "R", "x") is False
    assert send_direct_message("t", "a@b.c", "x") is False

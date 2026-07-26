"""The Webex REST helpers, driven through a mock transport.

These wrap the calls the bot makes for every reply: post, edit, delete, DM, and
the inbound-file download. Each has to degrade to a falsy result rather than raise,
because a Webex hiccup must not take the websocket loop down with it.
"""

from __future__ import annotations

import httpx
import pytest

from kai.chat import webex as wx

_REAL_CLIENT = httpx.Client


def _patch(monkeypatch, handler) -> None:
    """Route the module's httpx calls through ``handler``."""

    client = _REAL_CLIENT(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(wx.httpx, "post", client.post)
    monkeypatch.setattr(wx.httpx, "put", client.put)
    monkeypatch.setattr(wx.httpx, "get", client.get)
    # `client.delete`, not `client.request`: the latter takes (method, url) and
    # would raise on delete_message's (url, headers=...) call, silently turning
    # every delete into the except branch.
    monkeypatch.setattr(wx.httpx, "delete", client.delete)
    monkeypatch.setattr(wx.httpx, "stream", client.stream)


def test_create_message_returns_the_new_message_id(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer tok"
        return httpx.Response(200, json={"id": "msg-1"})

    _patch(monkeypatch, handler)
    assert wx.create_message("tok", roomId="r", markdown="hi") == "msg-1"


@pytest.mark.parametrize("status", [400, 401, 429, 500])
def test_create_message_returns_none_on_an_error(monkeypatch, status) -> None:
    _patch(monkeypatch, lambda r: httpx.Response(status, text="nope"))
    assert wx.create_message("tok", roomId="r", markdown="hi") is None


def test_create_message_survives_a_network_error(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    _patch(monkeypatch, handler)
    assert wx.create_message("tok", roomId="r", markdown="hi") is None


def test_send_direct_message_reports_success_and_failure(monkeypatch) -> None:
    _patch(monkeypatch, lambda r: httpx.Response(200, json={"id": "m"}))
    assert wx.send_direct_message("tok", "a@b.co", "hi") is True

    _patch(monkeypatch, lambda r: httpx.Response(404, text="no such person"))
    assert wx.send_direct_message("tok", "a@b.co", "hi") is False


def test_get_message_files_returns_the_attachment_urls(monkeypatch) -> None:
    _patch(monkeypatch, lambda r: httpx.Response(200, json={"files": ["http://f/1"]}))
    assert wx.get_message_files("tok", "m1") == ["http://f/1"]


def test_get_message_files_is_empty_when_the_lookup_fails(monkeypatch) -> None:
    _patch(monkeypatch, lambda r: httpx.Response(500))
    assert wx.get_message_files("tok", "m1") == []


def test_get_message_parent_falls_back_to_the_message_itself(monkeypatch) -> None:
    _patch(monkeypatch, lambda r: httpx.Response(200, json={"parentId": "root-1"}))
    assert wx.get_message_parent("tok", "m1") == "root-1"

    _patch(monkeypatch, lambda r: httpx.Response(200, json={}))
    assert wx.get_message_parent("tok", "m1") == "m1"


@pytest.mark.parametrize("status", [200, 204])
def test_delete_message_reports_success(monkeypatch, status) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.method)
        return httpx.Response(status)

    _patch(monkeypatch, handler)
    assert wx.delete_message("tok", "m1") is True
    assert seen == ["DELETE"], "the helper must actually issue a DELETE"


def test_delete_message_reports_failure(monkeypatch) -> None:
    _patch(monkeypatch, lambda r: httpx.Response(404))
    assert wx.delete_message("tok", "m1") is False


def test_delete_message_survives_a_network_error(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    _patch(monkeypatch, handler)
    assert wx.delete_message("tok", "m1") is False


def test_edit_message_updates_and_reports_failure(monkeypatch) -> None:
    methods: list[str] = []

    def ok(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        return httpx.Response(200, json={"id": "m1"})

    _patch(monkeypatch, ok)
    assert wx.edit_message("tok", "m1", "room", "new text") is True
    assert methods == ["PUT"]

    _patch(monkeypatch, lambda r: httpx.Response(409, text="cannot edit"))
    assert wx.edit_message("tok", "m1", "room", "x") is False


def test_download_file_returns_the_filename_and_bytes(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"hello",
            headers={"Content-Disposition": 'attachment; filename="notes.txt"'},
        )

    _patch(monkeypatch, handler)
    assert wx.download_file("tok", "http://f/1") == ("notes.txt", b"hello")


def test_download_file_returns_none_on_an_error(monkeypatch) -> None:
    _patch(monkeypatch, lambda r: httpx.Response(403))
    assert wx.download_file("tok", "http://f/1") is None


def test_download_file_refuses_a_body_over_the_cap(monkeypatch) -> None:
    """The cap has to hold even when the server declares no Content-Length."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 5000)

    _patch(monkeypatch, handler)
    assert wx.download_file("tok", "http://f/1", max_bytes=100) is None


# ======================================================================= #
# Pure rendering helpers
# ======================================================================= #
def test_feedback_card_offers_three_verdicts_on_a_confident_answer() -> None:
    card = wx.feedback_card("what is x?")
    assert card["type"] == "AdaptiveCard"
    assert {a["data"]["verdict"] for a in card["actions"]} == {"up", "down", "escalate"}
    assert all(a["data"]["question"] == "what is x?" for a in card["actions"])


def test_feedback_card_offers_only_escalate_on_an_escalation() -> None:
    card = wx.feedback_card("q", escalate_only=True)
    assert {a["data"]["verdict"] for a in card["actions"]} == {"escalate"}


def test_webex_reply_splits_a_long_answer_into_pieces() -> None:
    pieces, card = wx.webex_reply({"answer": "word " * 4000}, "q", show_card=False)
    assert len(pieces) > 1
    assert card is None
    assert all(len(p.encode()) <= 7439 for p in pieces)


def test_webex_reply_attaches_a_card_only_when_asked() -> None:
    _pieces, none_card = wx.webex_reply({"answer": "A."}, "q", show_card=False)
    _pieces, card = wx.webex_reply({"answer": "A."}, "q", show_card=True)
    assert none_card is None
    assert card is not None


def test_memory_key_separates_senders_in_one_thread() -> None:
    """Two people in one thread must not share follow-up context."""

    alice = {"actor": {"emailAddress": "alice@b.co"}}
    bob = {"actor": {"emailAddress": "bob@b.co"}}
    assert wx.memory_key("room", "parent", alice) != wx.memory_key("room", "parent", bob)
    assert wx.memory_key("room", "parent", alice) == ("parent", "alice@b.co")


def test_memory_key_falls_back_to_the_room_without_a_thread() -> None:
    assert wx.memory_key("room", None, {}) == ("room", "")

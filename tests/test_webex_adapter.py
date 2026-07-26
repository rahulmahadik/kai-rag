"""The Webex adapter's command handlers.

`run()` builds commands from the webex_bot SDK and then blocks in a supervised
reconnect loop. Injecting a fake `webex_bot` captures the command objects so their
`execute()` can be called directly, which is where all the real behaviour lives:
help routing, inbound-file Q&A, per-thread memory, edit-in-place, and feedback.
"""

from __future__ import annotations

import sys
import types
from typing import ClassVar

import pytest

import kai.chat.webex as wx
from kai.chat.webex import WebexAdapter
from kai.config import Settings


class FakeCommand:
    """Stands in for webex_bot.models.command.Command."""

    def __init__(self, **kw) -> None:
        self.kw = kw


class FakeResponse:
    def __init__(self) -> None:
        self.markdown = ""
        self.attachments = None


class FakeBot:
    """Captures the commands, then stops the supervised loop on run()."""

    instances: ClassVar[list[FakeBot]] = []

    def __init__(self, **kw) -> None:
        self.kw = kw
        self.commands = [kw.get("help_command")]
        FakeBot.instances.append(self)

    def add_command(self, cmd) -> None:
        self.commands.append(cmd)

    def run(self) -> None:
        raise KeyboardInterrupt  # ends run()'s reconnect loop deterministically


@pytest.fixture
def fake_sdk(monkeypatch):
    FakeBot.instances = []
    command_mod = types.ModuleType("webex_bot.models.command")
    command_mod.Command = FakeCommand
    response_mod = types.ModuleType("webex_bot.models.response")
    response_mod.Response = FakeResponse
    bot_mod = types.ModuleType("webex_bot.webex_bot")
    bot_mod.WebexBot = FakeBot
    for name, mod in [
        ("webex_bot", types.ModuleType("webex_bot")),
        ("webex_bot.models", types.ModuleType("webex_bot.models")),
        ("webex_bot.models.command", command_mod),
        ("webex_bot.models.response", response_mod),
        ("webex_bot.webex_bot", bot_mod),
    ]:
        monkeypatch.setitem(sys.modules, name, mod)


# These fields declare a validation_alias, so Settings only accepts the alias name.
_ALIASES = {
    "webex_bot_token": "WEBEX_BOT_TOKEN",
    "kai_api_url": "KAI_API_URL",
    "webex_approved_domains": "WEBEX_APPROVED_DOMAINS",
    "webex_approved_users": "WEBEX_APPROVED_USERS",
    "webex_feedback_card": "WEBEX_FEEDBACK_CARD",
}


def _settings(**over) -> Settings:
    base = {
        "WEBEX_BOT_TOKEN": "tok",
        "KAI_API_URL": "http://kai.local",
        "llm_timeout": 5,
        "webex_edit_in_place": False,  # the simpler path unless a case wants it
        "WEBEX_FEEDBACK_CARD": False,
    }
    base.update({_ALIASES.get(k, k): v for k, v in over.items()})
    return Settings(_env_file=None, **base)


def _activity(**over) -> dict:
    base = {
        "id": "msg-1",
        "roomId": "room-1",
        "target": {"globalId": "room-1"},
        "parent": {"id": "thread-1"},
        "actor": {"emailAddress": "alice@example.com"},
    }
    base.update(over)
    return base


@pytest.fixture
def commands(fake_sdk, monkeypatch):
    """Run the adapter and return (adapter, ask_command, feedback_command)."""

    def _build(**over):
        adapter = WebexAdapter(_settings(**over))
        # No network from any REST helper unless a case opts in.
        monkeypatch.setattr(wx, "get_message_files", lambda *a, **k: [])
        monkeypatch.setattr(wx, "create_message", lambda *a, **k: "posted-id")
        monkeypatch.setattr(wx, "edit_message", lambda *a, **k: True)
        monkeypatch.setattr(wx, "delete_message", lambda *a, **k: True)
        monkeypatch.setattr(wx, "get_message_parent", lambda *a, **k: "thread-1")
        with pytest.raises(KeyboardInterrupt):
            adapter.run()
        bot = FakeBot.instances[-1]
        return adapter, bot.commands[0], bot.commands[1]

    return _build


# ======================================================================= #
# Startup
# ======================================================================= #
def test_a_missing_token_exits_with_an_instruction(fake_sdk) -> None:
    with pytest.raises(SystemExit, match="WEBEX_BOT_TOKEN"):
        WebexAdapter(_settings(webex_bot_token="")).run()


def test_the_allowlist_is_passed_to_the_sdk(commands) -> None:
    _adapter, _ask, _fb = commands(
        webex_approved_domains="example.com, other.com", webex_approved_users="a@b.co"
    )
    kw = FakeBot.instances[-1].kw
    assert kw["approved_domains"] == ["example.com", "other.com"]
    assert kw["approved_users"] == ["a@b.co"]


def test_no_allowlist_means_the_sdk_gets_none(commands) -> None:
    commands()
    kw = FakeBot.instances[-1].kw
    assert kw["approved_domains"] is None and kw["approved_users"] is None


# ======================================================================= #
# Asking
# ======================================================================= #
def test_an_empty_message_gets_a_greeting_not_a_question(commands, monkeypatch) -> None:
    adapter, ask, _fb = commands()
    calls: list = []
    monkeypatch.setattr(adapter._service, "answer", lambda m: calls.append(1) or ({}, ""))

    out = ask.execute("   ", None, _activity())

    assert "Ask me anything" in out
    assert calls == []


def test_help_returns_the_help_text_without_asking(commands, monkeypatch) -> None:
    adapter, ask, _fb = commands()
    calls: list = []
    monkeypatch.setattr(adapter._service, "answer", lambda m: calls.append(1) or ({}, ""))

    out = ask.execute("help", None, _activity())

    assert "I'm KAI" in out
    assert calls == []


def test_a_question_is_answered_with_its_sources(commands, monkeypatch) -> None:
    adapter, ask, _fb = commands()
    monkeypatch.setattr(
        adapter._service,
        "answer",
        lambda m: (
            {
                "answer": "Rotate with the script [1].",
                "citations": [{"title": "Guide", "url": "http://kb/g"}],
                "escalated": False,
            },
            "",
        ),
    )

    out = ask.execute("how do I rotate keys?", None, _activity())

    text = out if isinstance(out, str) else out[0].markdown
    assert "Rotate with the script" in text
    assert "http://kb/g" in text


def test_an_api_error_is_returned_verbatim(commands, monkeypatch) -> None:
    adapter, ask, _fb = commands()
    monkeypatch.setattr(adapter._service, "answer", lambda m: (None, "KAI is unreachable."))

    assert ask.execute("q", None, _activity()) == "KAI is unreachable."


def test_the_reply_is_edited_in_place_when_enabled(commands, monkeypatch) -> None:
    adapter, ask, _fb = commands(webex_edit_in_place=True)
    edits: list[str] = []
    monkeypatch.setattr(wx, "create_message", lambda *a, **k: "ack-1")
    monkeypatch.setattr(wx, "edit_message", lambda tok, mid, room, md: edits.append(md) or True)
    monkeypatch.setattr(
        adapter._service, "answer", lambda m: ({"answer": "A [1].", "escalated": False}, "")
    )

    out = ask.execute("q", None, _activity())

    assert out is None, "the ack was edited, so the SDK must not post again"
    assert edits and "A [1]." in edits[0]


def test_an_escalation_is_not_framed_as_an_answer(commands, monkeypatch) -> None:
    """The 'Here's what I found' prefix must never sit above a refusal."""

    adapter, ask, _fb = commands(webex_edit_in_place=True, bot_answer_prefix="Here's what I found:")
    edits: list[str] = []
    monkeypatch.setattr(wx, "create_message", lambda *a, **k: "ack-1")
    monkeypatch.setattr(wx, "edit_message", lambda tok, mid, room, md: edits.append(md) or True)
    monkeypatch.setattr(
        adapter._service, "answer", lambda m: ({"answer": "I couldn't.", "escalated": True}, "")
    )

    ask.execute("q", None, _activity())

    assert "Here's what I found" not in edits[0]


def test_a_failed_edit_deletes_the_stale_ack_and_replies_normally(commands, monkeypatch) -> None:
    adapter, ask, _fb = commands(webex_edit_in_place=True)
    deleted: list[str] = []
    monkeypatch.setattr(wx, "create_message", lambda *a, **k: "ack-1")
    monkeypatch.setattr(wx, "edit_message", lambda *a, **k: False)
    monkeypatch.setattr(wx, "delete_message", lambda tok, mid: deleted.append(mid) or True)
    monkeypatch.setattr(
        adapter._service, "answer", lambda m: ({"answer": "A.", "escalated": False}, "")
    )

    out = ask.execute("q", None, _activity())

    assert deleted == ["ack-1"], "a dangling 'searching' ack would look stuck"
    assert out is not None


# ======================================================================= #
# Inbound file Q&A
# ======================================================================= #
def test_an_attached_file_is_answered_instead_of_the_corpus(commands, monkeypatch) -> None:
    adapter, ask, _fb = commands()
    corpus: list = []
    posted: list[str] = []
    monkeypatch.setattr(wx, "get_message_files", lambda *a, **k: ["http://f/1"])
    monkeypatch.setattr(wx, "download_file", lambda *a, **k: ("notes.pdf", b"data"))
    monkeypatch.setattr(wx, "create_message", lambda *a, **k: posted.append(k.get("markdown")))
    monkeypatch.setattr(adapter._service, "answer", lambda m: corpus.append(1) or ({}, ""))
    monkeypatch.setattr(
        adapter._service,
        "ask_document",
        lambda name, data, q: ({"answer": "From the file.", "escalated": False}, ""),
    )

    ask.execute("what does it say?", None, _activity())

    assert corpus == [], "an attached file must not fall through to corpus Q&A"
    assert posted and "From the file." in posted[0]
    assert "notes.pdf" in posted[0]
    assert "isn't stored" in posted[0]


def test_an_unreadable_attachment_says_so_rather_than_answering_anyway(
    commands, monkeypatch
) -> None:
    adapter, ask, _fb = commands()
    corpus: list = []
    monkeypatch.setattr(wx, "get_message_files", lambda *a, **k: ["http://f/1"])
    monkeypatch.setattr(wx, "download_file", lambda *a, **k: None)
    monkeypatch.setattr(adapter._service, "answer", lambda m: corpus.append(1) or ({}, ""))

    out = ask.execute("what does it say?", None, _activity())

    assert "couldn't read that attachment" in out
    assert corpus == [], "silently answering from the corpus would look like it was ignored"


def test_a_failure_to_list_attachments_falls_through_to_corpus_qa(commands, monkeypatch) -> None:
    adapter, ask, _fb = commands()
    asked: list = []

    def boom(*a, **k):
        raise RuntimeError("webex down")

    monkeypatch.setattr(wx, "get_message_files", boom)
    monkeypatch.setattr(
        adapter._service,
        "answer",
        lambda m: (asked.append(m.text), ({"answer": "A.", "escalated": False}, ""))[1],
    )

    ask.execute("a question", None, _activity())

    assert asked == ["a question"]


# ======================================================================= #
# Conversation memory
# ======================================================================= #
def test_a_referential_follow_up_carries_the_previous_topic(commands, monkeypatch) -> None:
    adapter, ask, _fb = commands(conversation_memory=True)
    asked: list[str] = []
    monkeypatch.setattr(
        adapter._service,
        "answer",
        lambda m: (asked.append(m.text), ({"answer": "A.", "escalated": False}, ""))[1],
    )

    ask.execute("how does replication work?", None, _activity())
    ask.execute("what about failover?", None, _activity())

    assert asked[0] == "how does replication work?"
    assert "in the context of: how does replication work?" in asked[1]


def test_a_standalone_follow_up_is_left_alone(commands, monkeypatch) -> None:
    adapter, ask, _fb = commands(conversation_memory=True)
    asked: list[str] = []
    monkeypatch.setattr(
        adapter._service,
        "answer",
        lambda m: (asked.append(m.text), ({"answer": "A.", "escalated": False}, ""))[1],
    )

    ask.execute("how does replication work?", None, _activity())
    ask.execute("what is the retention policy for audit logs?", None, _activity())

    assert "in the context of" not in asked[1]


def test_two_people_in_one_thread_keep_separate_context(commands, monkeypatch) -> None:
    adapter, ask, _fb = commands(conversation_memory=True)
    asked: list[str] = []
    monkeypatch.setattr(
        adapter._service,
        "answer",
        lambda m: (asked.append(m.text), ({"answer": "A.", "escalated": False}, ""))[1],
    )
    alice = _activity(actor={"emailAddress": "alice@example.com"})
    bob = _activity(actor={"emailAddress": "bob@example.com"})

    ask.execute("how does replication work?", None, alice)
    ask.execute("what about it?", None, bob)

    assert "in the context of" not in asked[1], "bob must not inherit alice's topic"


def test_memory_is_off_by_default(commands, monkeypatch) -> None:
    adapter, ask, _fb = commands()
    asked: list[str] = []
    monkeypatch.setattr(
        adapter._service,
        "answer",
        lambda m: (asked.append(m.text), ({"answer": "A.", "escalated": False}, ""))[1],
    )

    ask.execute("how does replication work?", None, _activity())
    ask.execute("what about failover?", None, _activity())

    assert "in the context of" not in asked[1]


# ======================================================================= #
# Feedback card
# ======================================================================= #
class _Actions:
    def __init__(self, verdict: str, question: str) -> None:
        self.inputs = {"verdict": verdict, "question": question}
        self.messageId = "card-1"
        self.roomId = "room-1"


def test_a_card_tap_is_routed_and_acknowledged_in_thread(commands, monkeypatch) -> None:
    adapter, _ask, fb = commands()
    seen: list = []
    posted: list[dict] = []
    monkeypatch.setattr(
        adapter._service, "handle_feedback", lambda e: seen.append(e) or "Thanks, noted."
    )
    monkeypatch.setattr(wx, "create_message", lambda tok, **k: posted.append(k))

    out = fb.execute(None, _Actions("down", "what is x?"), _activity())

    assert seen[0].verdict == "down"
    assert seen[0].question == "what is x?"
    assert seen[0].sender_email == "alice@example.com"
    assert posted[0]["parentId"] == "thread-1", "the ack belongs in the original thread"
    assert out is None


def test_the_tapped_card_is_dismissed_so_it_cannot_be_submitted_twice(
    commands, monkeypatch
) -> None:
    adapter, _ask, fb = commands()
    deleted: list[str] = []
    monkeypatch.setattr(adapter._service, "handle_feedback", lambda e: "Thanks.")
    monkeypatch.setattr(wx, "delete_message", lambda tok, mid: deleted.append(mid) or True)

    fb.execute(None, _Actions("up", "q"), _activity())

    assert deleted == ["card-1"]


def test_a_card_tap_without_an_actor_still_records_the_feedback(commands, monkeypatch) -> None:
    adapter, _ask, fb = commands()
    seen: list = []
    monkeypatch.setattr(adapter._service, "handle_feedback", lambda e: seen.append(e) or "ok")

    fb.execute(None, _Actions("up", "q"), {"id": "m"})

    assert seen and seen[0].sender_email == ""


# ======================================================================= #
# Supervised reconnect
# ======================================================================= #
def test_a_crash_loop_backs_off_instead_of_reconnecting_flat_out(fake_sdk, monkeypatch) -> None:
    """Regression: the base delay used to be reset on every pass, before bot.run(),
    which pinned the wait at 5s forever and hammered Webex through an outage."""

    slept: list[float] = []
    clock = {"t": 0.0}

    class _Crashing:
        def __init__(self, **kw) -> None:
            pass

        def add_command(self, cmd) -> None:
            pass

        def run(self) -> None:
            raise RuntimeError("websocket died immediately")

    monkeypatch.setattr(sys.modules["webex_bot.webex_bot"], "WebexBot", _Crashing)
    monkeypatch.setattr(wx.time, "monotonic", lambda: clock["t"])

    def _sleep(seconds: float) -> None:
        slept.append(seconds)
        clock["t"] += seconds
        if len(slept) >= 5:
            raise KeyboardInterrupt

    monkeypatch.setattr(wx.time, "sleep", _sleep)

    with pytest.raises(KeyboardInterrupt):
        WebexAdapter(_settings()).run()

    assert slept == [5.0, 10.0, 20.0, 40.0, 80.0], f"backoff did not grow: {slept}"


def test_a_session_that_stayed_up_resets_the_backoff(fake_sdk, monkeypatch) -> None:
    """A bot that ran for a while and then dropped should retry promptly, not
    inherit the delay from an earlier bad patch."""

    slept: list[float] = []
    clock = {"t": 0.0}
    runs = {"n": 0}

    class _Flaky:
        def __init__(self, **kw) -> None:
            pass

        def add_command(self, cmd) -> None:
            pass

        def run(self) -> None:
            runs["n"] += 1
            # First two sessions die at once; the third stays up past the healthy mark.
            if runs["n"] == 3:
                clock["t"] += wx._RECONNECT_HEALTHY_RUN + 1
            raise RuntimeError("dropped")

    monkeypatch.setattr(sys.modules["webex_bot.webex_bot"], "WebexBot", _Flaky)
    monkeypatch.setattr(wx.time, "monotonic", lambda: clock["t"])

    def _sleep(seconds: float) -> None:
        slept.append(seconds)
        clock["t"] += seconds
        if len(slept) >= 4:
            raise KeyboardInterrupt

    monkeypatch.setattr(wx.time, "sleep", _sleep)

    with pytest.raises(KeyboardInterrupt):
        WebexAdapter(_settings()).run()

    assert slept == [5.0, 10.0, 5.0, 10.0], f"a healthy run should reset: {slept}"


def test_the_backoff_is_capped(fake_sdk, monkeypatch) -> None:
    slept: list[float] = []

    class _Crashing:
        def __init__(self, **kw) -> None:
            pass

        def add_command(self, cmd) -> None:
            pass

        def run(self) -> None:
            raise RuntimeError("down")

    monkeypatch.setattr(sys.modules["webex_bot.webex_bot"], "WebexBot", _Crashing)
    monkeypatch.setattr(wx.time, "monotonic", lambda: 0.0)

    def _sleep(seconds: float) -> None:
        slept.append(seconds)
        if len(slept) >= 12:
            raise KeyboardInterrupt

    monkeypatch.setattr(wx.time, "sleep", _sleep)

    with pytest.raises(KeyboardInterrupt):
        WebexAdapter(_settings()).run()

    assert max(slept) == wx._RECONNECT_MAX
    assert slept[-1] == wx._RECONNECT_MAX


def test_a_service_error_is_edited_into_the_live_ack(commands, monkeypatch) -> None:
    """With an ack already posted, the error must replace it rather than arrive as a
    second message under a dangling 'Searching...'."""

    adapter, ask, _fb = commands(webex_edit_in_place=True)
    edits: list[str] = []
    monkeypatch.setattr(wx, "create_message", lambda *a, **k: "ack-1")
    monkeypatch.setattr(wx, "edit_message", lambda tok, mid, room, md: edits.append(md) or True)
    monkeypatch.setattr(adapter._service, "answer", lambda m: (None, "KAI is unreachable."))

    out = ask.execute("q", None, _activity())

    assert out is None
    assert edits == ["KAI is unreachable."]


def test_pre_execute_acks_only_when_not_editing_in_place(commands) -> None:
    _adapter, ask, _fb = commands()
    assert "searching" in ask.pre_execute("a real question", None, _activity()).lower()
    assert ask.pre_execute("   ", None, _activity()) is None


def test_pre_execute_is_silent_when_editing_in_place(commands) -> None:
    _adapter, ask, _fb = commands(webex_edit_in_place=True)
    assert ask.pre_execute("a real question", None, _activity()) is None

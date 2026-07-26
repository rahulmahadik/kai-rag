"""The Slack adapter's startup checks and its registered handlers.

`run()` builds a slack_bolt App, registers handlers as decorators, then blocks in
SocketModeHandler.start(). Injecting a fake `slack_bolt` into sys.modules lets the
real registration run and captures the handlers so they can be invoked directly.
"""

from __future__ import annotations

import sys
import types
from typing import ClassVar

import pytest

from kai.chat.slack import SlackAdapter, _slack_start_hint, collapse_feedback_blocks
from kai.config import Settings


class FakeApp:
    """Records every handler slack_bolt would have registered."""

    def __init__(self, token: str = "") -> None:
        self.token = token
        self.events: dict[str, callable] = {}
        self.actions: dict[str, callable] = {}

    def event(self, name):
        def deco(fn):
            self.events[name] = fn
            return fn

        return deco

    def action(self, action_id):
        def deco(fn):
            self.actions[action_id] = fn
            return fn

        return deco


class FakeSocketHandler:
    started: ClassVar[list[str]] = []

    def __init__(self, app, app_token) -> None:
        self.app, self.app_token = app, app_token

    def start(self) -> None:
        FakeSocketHandler.started.append(self.app_token)


class FakeSlackApiError(Exception):
    def __init__(self, msg="", response=None) -> None:
        super().__init__(msg)
        self.response = response


@pytest.fixture
def fake_slack(monkeypatch):
    """Install a fake slack_bolt / slack_sdk so run() can execute offline."""

    built: dict = {}

    def _app(token=""):
        app = FakeApp(token)
        built["app"] = app
        return app

    bolt = types.ModuleType("slack_bolt")
    bolt.App = _app
    adapter_mod = types.ModuleType("slack_bolt.adapter")
    socket_mod = types.ModuleType("slack_bolt.adapter.socket_mode")
    socket_mod.SocketModeHandler = FakeSocketHandler
    sdk = types.ModuleType("slack_sdk")
    errors = types.ModuleType("slack_sdk.errors")
    errors.SlackApiError = FakeSlackApiError

    for name, mod in [
        ("slack_bolt", bolt),
        ("slack_bolt.adapter", adapter_mod),
        ("slack_bolt.adapter.socket_mode", socket_mod),
        ("slack_sdk", sdk),
        ("slack_sdk.errors", errors),
    ]:
        monkeypatch.setitem(sys.modules, name, mod)

    FakeSocketHandler.started = []
    return built


def _settings(**over) -> Settings:
    base = {
        "slack_bot_token": "xoxb-real",
        "slack_app_token": "xapp-real",
        "kai_api_url": "http://kai.local",
        "llm_timeout": 5,
    }
    base.update(over)
    return Settings(_env_file=None, **base)


@pytest.fixture
def running(fake_slack, monkeypatch):
    """A started adapter, plus the captured app and a `say` recorder."""

    adapter = SlackAdapter(_settings())
    said: list[dict] = []
    adapter.run()
    return adapter, fake_slack["app"], said


# ======================================================================= #
# Startup validation
# ======================================================================= #
def test_missing_tokens_exit_with_an_instruction(fake_slack) -> None:
    with pytest.raises(SystemExit, match="SLACK_BOT_TOKEN"):
        SlackAdapter(_settings(slack_bot_token="")).run()


@pytest.mark.parametrize(
    ("bot", "app", "match"),
    [
        ("wrong-prefix", "xapp-x", "SLACK_BOT_TOKEN must start with 'xoxb-'"),
        ("xoxb-x", "wrong-prefix", "SLACK_APP_TOKEN must start with 'xapp-'"),
    ],
)
def test_a_wrong_token_prefix_is_caught_before_connecting(fake_slack, bot, app, match) -> None:
    with pytest.raises(SystemExit, match=match):
        SlackAdapter(_settings(slack_bot_token=bot, slack_app_token=app)).run()


def test_swapped_tokens_say_which_way_round_they_go(fake_slack) -> None:
    """The two tokens are easy to transpose, and slack_bolt's own error is cryptic."""

    with pytest.raises(SystemExit, match="That is the App-Level token"):
        SlackAdapter(_settings(slack_bot_token="xapp-a", slack_app_token="xapp-b")).run()

    with pytest.raises(SystemExit, match="That is the Bot token"):
        SlackAdapter(_settings(slack_bot_token="xoxb-a", slack_app_token="xoxb-b")).run()


def test_run_connects_socket_mode_with_the_app_token(running) -> None:
    assert FakeSocketHandler.started == ["xapp-real"]


def test_run_registers_the_expected_handlers(running) -> None:
    _adapter, app, _said = running
    assert set(app.events) == {"app_mention", "message"}
    assert set(app.actions) == {"kai_fb_up", "kai_fb_down", "kai_fb_escalate"}


# ======================================================================= #
# Message handling
# ======================================================================= #
def test_a_mention_is_stripped_before_the_question_is_sent(running, monkeypatch) -> None:
    adapter, app, said = running
    asked: list[str] = []
    monkeypatch.setattr(
        adapter._service,
        "answer",
        lambda msg: (asked.append(msg.text), ({"answer": "A."}, ""))[1],
    )

    app.events["app_mention"](
        {"text": "<@U123> how do I rotate keys?", "channel": "C1", "ts": "1.0"},
        lambda **kw: said.append(kw),
    )

    assert asked == ["how do I rotate keys?"]


def test_a_reply_is_threaded_under_the_original_message(running, monkeypatch) -> None:
    adapter, app, said = running
    monkeypatch.setattr(adapter._service, "answer", lambda msg: ({"answer": "A."}, ""))

    app.events["app_mention"](
        {"text": "q", "channel": "C1", "ts": "1.0", "thread_ts": "0.5"},
        lambda **kw: said.append(kw),
    )

    assert said and said[0]["thread_ts"] == "0.5"
    assert said[0]["channel"] == "C1"


def test_an_empty_message_is_ignored(running, monkeypatch) -> None:
    adapter, app, said = running
    calls: list = []
    monkeypatch.setattr(adapter._service, "answer", lambda msg: calls.append(1) or ({}, ""))

    app.events["app_mention"](
        {"text": "<@U123>   ", "channel": "C1", "ts": "1"}, lambda **kw: said.append(kw)
    )

    assert calls == [] and said == []


def test_help_is_answered_locally(running, monkeypatch) -> None:
    adapter, app, said = running
    calls: list = []
    monkeypatch.setattr(adapter._service, "answer", lambda msg: calls.append(1) or ({}, ""))

    app.events["app_mention"](
        {"text": "help", "channel": "C1", "ts": "1"}, lambda **kw: said.append(kw)
    )

    assert calls == [], "help must not cost an API call"
    assert "KAI" in said[0]["text"]


def test_an_api_error_is_relayed_to_the_channel(running, monkeypatch) -> None:
    adapter, app, said = running
    monkeypatch.setattr(adapter._service, "answer", lambda msg: (None, "KAI is unreachable."))

    app.events["app_mention"](
        {"text": "q", "channel": "C1", "ts": "1"}, lambda **kw: said.append(kw)
    )

    assert said[0]["text"] == "KAI is unreachable."


def test_a_direct_message_is_answered_but_the_bots_own_are_not(running, monkeypatch) -> None:
    adapter, app, said = running
    calls: list = []
    monkeypatch.setattr(
        adapter._service, "answer", lambda msg: (calls.append(msg.text), ({"answer": "A."}, ""))[1]
    )

    app.events["message"](
        {"text": "hello", "channel": "D1", "ts": "1", "channel_type": "im"},
        lambda **kw: said.append(kw),
    )
    app.events["message"](
        {"text": "loop", "channel": "D1", "ts": "2", "channel_type": "im", "bot_id": "B1"},
        lambda **kw: said.append(kw),
    )
    app.events["message"](
        {"text": "channel chatter", "channel": "C1", "ts": "3", "channel_type": "channel"},
        lambda **kw: said.append(kw),
    )

    assert calls == ["hello"], "only non-bot DMs are answered"


def test_a_long_answer_is_split_and_only_the_last_piece_carries_buttons(
    running, monkeypatch
) -> None:
    adapter, app, said = running
    long = "\n\n".join(f"para {i} " + "word " * 80 for i in range(20))
    monkeypatch.setattr(adapter._service, "answer", lambda msg: ({"answer": long}, ""))

    app.events["app_mention"](
        {"text": "q", "channel": "C1", "ts": "1"}, lambda **kw: said.append(kw)
    )

    assert len(said) > 1
    assert not any(b["type"] == "actions" for b in said[0]["blocks"])
    assert any(b["type"] == "actions" for b in said[-1]["blocks"])


# ======================================================================= #
# Feedback taps
# ======================================================================= #
def _body(question: str = "q") -> dict:
    return {
        "actions": [{"value": question}],
        "user": {"username": "alice"},
        "message": {"ts": "1.0", "blocks": [{"block_id": "kai_feedback"}], "thread_ts": "0.5"},
        "channel": {"id": "C1"},
    }


class FakeClient:
    def __init__(self, fail: bool = False) -> None:
        self.updates: list[dict] = []
        self.fail = fail

    def chat_update(self, **kw):
        if self.fail:
            raise RuntimeError("slack down")
        self.updates.append(kw)


@pytest.mark.parametrize("verdict", ["up", "down", "escalate"])
def test_each_feedback_button_routes_its_verdict(running, monkeypatch, verdict) -> None:
    adapter, app, _said = running
    seen: list = []
    monkeypatch.setattr(
        adapter._service, "handle_feedback", lambda fb: seen.append(fb) or "Thanks."
    )
    client = FakeClient()

    app.actions[f"kai_fb_{verdict}"](lambda: None, _body("what is x?"), client, lambda **kw: None)

    assert seen[0].verdict == verdict
    assert seen[0].question == "what is x?"
    assert seen[0].sender_email == "alice"


def test_tapping_a_button_removes_it_so_feedback_cannot_be_resubmitted(
    running, monkeypatch
) -> None:
    adapter, app, _said = running
    monkeypatch.setattr(adapter._service, "handle_feedback", lambda fb: "Thanks.")
    client = FakeClient()

    app.actions["kai_fb_up"](lambda: None, _body(), client, lambda **kw: None)

    blocks = client.updates[0]["blocks"]
    assert not any(str(b.get("block_id", "")).startswith("kai_feedback") for b in blocks)
    assert "Thanks." in str(blocks[-1])


def test_a_failed_update_falls_back_to_a_thread_note(running, monkeypatch) -> None:
    adapter, app, _said = running
    monkeypatch.setattr(adapter._service, "handle_feedback", lambda fb: "Thanks.")
    posted: list[dict] = []

    app.actions["kai_fb_up"](
        lambda: None, _body(), FakeClient(fail=True), lambda **kw: posted.append(kw)
    )

    assert posted and posted[0]["text"] == "Thanks."
    assert posted[0]["thread_ts"] == "0.5"


# ======================================================================= #
# Pure helpers
# ======================================================================= #
def test_collapse_keeps_non_feedback_blocks_and_appends_the_confirmation() -> None:
    blocks = [
        {"type": "section", "block_id": "answer"},
        {"type": "context", "block_id": "kai_feedback_prompt"},
        {"type": "actions", "block_id": "kai_feedback"},
    ]

    out = collapse_feedback_blocks(blocks, "Thanks!")

    assert [b.get("block_id") for b in out[:-1]] == ["answer"]
    assert out[-1]["elements"][0]["text"] == "Thanks!"


def test_collapse_handles_an_empty_block_list() -> None:
    assert len(collapse_feedback_blocks([], "ok")) == 1


@pytest.mark.parametrize(
    ("response", "expect"),
    [
        ({"error": "missing_scope", "needed": "connections:write"}, "connections:write"),
        ({"error": "invalid_auth"}, "invalid_auth"),
        ({"error": "token_revoked"}, "token_revoked"),
        ({"error": "account_inactive"}, "account_inactive"),
    ],
)
def test_a_known_startup_error_becomes_a_one_line_fix(response, expect) -> None:
    hint = _slack_start_hint(FakeSlackApiError(response=response))
    assert hint and expect in hint


def test_an_unknown_startup_error_is_re_raised_not_guessed_at() -> None:
    assert _slack_start_hint(FakeSlackApiError(response={"error": "weird_new_thing"})) is None
    assert _slack_start_hint(RuntimeError("no response attr")) is None


def test_a_recognised_socket_mode_failure_becomes_a_readable_exit(fake_slack, monkeypatch) -> None:
    """slack_bolt's own error on a bad app token is cryptic; startup should say
    exactly which token to regenerate."""

    def _start(self) -> None:
        raise FakeSlackApiError(response={"error": "missing_scope", "needed": "connections:write"})

    monkeypatch.setattr(FakeSocketHandler, "start", _start)

    with pytest.raises(SystemExit, match="connections:write"):
        SlackAdapter(_settings()).run()


def test_an_unrecognised_socket_mode_failure_is_re_raised_untouched(
    fake_slack, monkeypatch
) -> None:
    """Never swallow an error we do not have advice for."""

    def _start(self) -> None:
        raise FakeSlackApiError("boom", response={"error": "some_new_error"})

    monkeypatch.setattr(FakeSocketHandler, "start", _start)

    with pytest.raises(FakeSlackApiError):
        SlackAdapter(_settings()).run()

"""Multi-user concurrency: many users hitting the bot at once (e.g. in one group)
must each get THEIR OWN answer — the shared ChatService must be stateless/re-entrant,
with no cross-talk. (Per-thread reply targeting is the adapter's job and uses
per-message local state; this pins the shared 'brain'.)"""

from __future__ import annotations

import threading
import time

from kai.chat.base import IncomingMessage
from kai.chat.service import ChatService
from kai.config import Settings


class _Resp:
    def __init__(self, question: str) -> None:
        self._q = question
        self.status_code = 200

    def json(self) -> dict:
        return {"answer": self._q, "citations": [], "confidence": 0.9, "escalated": False}


def test_concurrent_users_each_get_their_own_answer(monkeypatch):
    # Echo the question back as the answer, with a tiny delay to force interleaving.
    def fake_post(url, json, headers, timeout):  # noqa: ANN001
        time.sleep(0.005)
        return _Resp(json["question"])

    monkeypatch.setattr("kai.chat.service.httpx.post", fake_post)
    svc = ChatService(Settings(_env_file=None, kai_api_url="http://x", llm_timeout=1))

    results: dict[int, str] = {}

    def worker(i: int) -> None:
        data, _ = svc.answer(IncomingMessage(text=f"user-{i}-question"))
        results[i] = data["answer"]

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(25)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Every concurrent caller got exactly its own answer — no cross-contamination.
    assert results == {i: f"user-{i}-question" for i in range(25)}


def test_incoming_message_carries_its_own_thread_id():
    # The adapter passes each message's thread id through, so replies target the
    # right thread per user (no shared parent across concurrent messages).
    a = IncomingMessage(text="q1", thread_id="thread-A")
    b = IncomingMessage(text="q2", thread_id="thread-B")
    assert a.thread_id == "thread-A" and b.thread_id == "thread-B"


def test_webex_memory_key_isolates_users_in_same_thread():
    # Two people in ONE Webex thread must get SEPARATE follow-up context: the
    # conversation-memory key includes the sender, so user A's last question can
    # never enrich (leak into) user B's follow-up.
    from kai.chat.webex import memory_key

    room, parent = "ROOM1", "THREAD1"
    a = {"actor": {"emailAddress": "alice@corp.com"}}
    b = {"actor": {"emailAddress": "bob@corp.com"}}
    ka, kb = memory_key(room, parent, a), memory_key(room, parent, b)
    assert ka != kb  # same thread, different senders → distinct memory slots
    assert ka == ("THREAD1", "alice@corp.com")
    # Same user in two different threads is also isolated.
    assert memory_key(room, "THREAD2", a) != ka
    # Missing actor degrades safely (no crash, anonymous slot).
    assert memory_key(room, parent, {}) == ("THREAD1", "")

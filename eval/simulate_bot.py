#!/usr/bin/env python3
"""Dry-run the chat bots WITHOUT platform tokens.

Drives real conversations through the live /ask backend and the EXACT render
code each adapter uses (webex_reply / slack_messages / handle_feedback /
ask_document), printing what Webex and Slack would send. The only thing this
does NOT cover is the literal platform socket (receiving the event + the
platform rendering the sent message) — that needs real tokens.

    python run/setup.py start        # backend must be up
    .venv/bin/python eval/simulate_bot.py
"""

from kai.config import Settings
from kai.chat.service import ChatService, format_reply
from kai.chat.base import IncomingMessage, FeedbackEvent
from kai.chat.webex import webex_reply
from kai.chat.slack import slack_messages

s = Settings(kai_api_url="http://127.0.0.1:8100")  # REAL backend
svc = ChatService(s)


def turn(label, q):
    data, err = svc.answer(IncomingMessage(text=q))
    print(f"\n>>> USER: {q!r}   [{label}]")
    if data is None:
        print("    (error reply):", err)
        return
    # exactly what each adapter would SEND:
    wpieces, wcard = webex_reply(data, q, show_card=True)
    print(
        f"    WEBEX  -> {len(wpieces)} msg, card={'yes' if wcard else 'no'} | {wpieces[0][:90]!r}"
    )
    smsgs = slack_messages(format_reply(data), q, escalated=data["escalated"], show_buttons=True)
    sb = [b["type"] for b in smsgs[-1]["blocks"]]
    print(f"    SLACK  -> {len(smsgs)} msg, blocks={sb} | {smsgs[0]['text'][:90]!r}")


print("============ SIMULATED CONVERSATION (real /ask backend, real render code) ============")
turn("in-scope", "What is Kafka?")
turn("imperative phrasing", "show me details of RFC 1918")
turn("out-of-scope -> escalate + suggested sources", "what is the capital of France?")
turn("fabrication bait -> escalate", "what exact SSL keystore password do the brokers use?")

print("\n--- FEEDBACK routing (what the button taps do) ---")
for v in ("up", "down", "escalate"):
    print(
        f"    👆 {v:8s} -> {svc.handle_feedback(FeedbackEvent(verdict=v, question='What is Kafka?'))!r}"
    )

print("\n--- INBOUND FILE (drop a file, ask about it) — the ad-hoc RAG path the bot calls ---")
data = open("samples/01_kai_overview.pdf", "rb").read()
doc, err = svc.ask_document("01_kai_overview.pdf", data, "What is KAI's core guarantee?")
print(
    "    file Q ->",
    "ESC/not-found" if (doc is None or doc["escalated"]) else f"ANS: {doc['answer'][:80]!r}",
)

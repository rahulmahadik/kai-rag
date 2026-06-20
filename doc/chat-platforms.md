# Chat platforms — one abstraction, many surfaces

KAI's chat bot is a **thin client over the platform-agnostic HTTP API**
(`POST /ask` / `/feedback` / `/escalate`). Adding a new enterprise chat platform
(Slack, Teams, …) does **not** touch the pipeline, the API, or the retrieval/guard
logic — it's one new adapter file.

## The seam

```
chat platform (Webex/Slack/Teams)
        │  receive @mention / card action
        ▼
  ChatAdapter (kai/chat/<platform>.py)   ← transport-specific (SDK, size limits, card format)
        │  IncomingMessage / FeedbackEvent
        ▼
  ChatService (kai/chat/service.py)      ← PLATFORM-NEUTRAL: call /ask, format reply, route feedback
        │  HTTP
        ▼
  KAI API (/ask, /feedback, /escalate)   ← unchanged; the real seam
```

- `kai/chat/base.py` — `ChatAdapter` Protocol + `IncomingMessage` / `FeedbackEvent`.
- `kai/chat/service.py` — `ChatService` + portable `format_reply` / `split_message`.
- `kai/chat/webex.py`, `kai/chat/slack.py` — adapters. `kai/chat/teams.py` — recipe.
- Selected by `CHAT_PLATFORM` (`webex` default | `slack` | `teams`).

## Platform status

| Platform | Transport | Public URL? | Status | Run |
| --- | --- | --- | --- | --- |
| **Webex** | outbound websocket | no | **shipped, validated** | `python -m kai.bot` |
| **Slack** | Socket Mode (websocket) | no | **complete, needs tokens to test** | `pip install -e '.[slack]'`, set `SLACK_BOT_TOKEN`+`SLACK_APP_TOKEN`, `CHAT_PLATFORM=slack python -m kai.bot` |
| **Teams** | inbound HTTPS webhook (Azure Bot) | **yes** | included; verify in your Azure tenant | `pip install '.[teams]'`, set `TEAMS_APP_ID`+`TEAMS_APP_PASSWORD`, `CHAT_PLATFORM=teams python -m kai.bot` |

Teams is the only one that inverts the transport (needs a public endpoint + Azure
Bot Service). The adapter is **included** (`kai/chat/teams.py`) and its parsing/routing
is unit-tested (`tests/test_teams.py`); the live Connector + Bot-Framework-auth
round-trip can only be exercised in a real Azure tenant, so verify it there.

## Add a platform in 3 steps

1. `kai/chat/<platform>.py`: a class with `name` + `run()` that satisfies
   `ChatAdapter` — receive messages → `ChatService.answer(IncomingMessage(...))` →
   render markdown (split to the platform's limit) → reply (in-thread); route
   button/card actions → `ChatService.handle_feedback(FeedbackEvent(...))`.
2. Register it in `kai/chat/__init__.py:build_chat_adapter`.
3. Add its tokens to `kai/config.py` + `.env.example`. Done — no pipeline change.

Not available on Webex (don't design around them): native message **reactions**
and **ephemeral** messages have no bot API — the Adaptive Card feedback and a 1:1
DM are the sanctioned substitutes.

## Webex enhancements (shipped)

* **Inbound file Q&A** — a user @mentions the bot with a file attached; KAI downloads
  it (size-capped), runs ad-hoc RAG over **that file only** via `/ask-document`, and
  replies in-thread with a "read just for this question, not saved" note. Reuses the
  `kai/providers/file_source.py` extraction. (Slack/Teams don't handle attachments yet.)
* **Edit-in-place** (`WEBEX_EDIT_IN_PLACE=true`, default) — the "🔎 Searching…" ack
  message is EDITED into "**Here's what I found:** …" via `PUT /messages/{id}`,
  instead of delete-then-repost. No notification churn, keeps thread position.
  Overflow pieces + the feedback card post as in-thread follow-ups (a card-bearing
  message can't be edited). Falls back to a normal reply if the REST edit fails.
* **Proactive DM** — `POST /notify {email, message}` (API, auth-gated) DMs a user
  via `toPersonEmail`. Use it to tell the asker when their escalation is resolved
  (the Inform loop calls this automatically on approval). Webex rule: the recipient
  must have messaged the bot before.
* **Conversation memory** (`CONVERSATION_MEMORY=true`, EXPERIMENTAL, default off) —
  prepends the last question's topic to a referential follow-up ("what about X?")
  so it retrieves in context. The enriched query still runs the FULL gate + guards,
  so context can never make an out-of-scope question fabricate. Needs live chat
  testing before enabling.

## Why NOT token streaming (deliberate)

SSE token-streaming is intentionally **not** implemented: KAI's grounding/verify
guards run AFTER generation, so streaming raw tokens would show the user text
before it is validated — a fabrication path that breaks the #1 "never confidently
wrong" rule. Edit-in-place is the safe responsiveness win instead (the user sees
progress immediately and the FULL validated answer when ready).

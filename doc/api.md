# API reference & request flow

KAI is **one HTTP service**. Everything — the web UI, the Webex/Slack bots, your own
scripts — talks to the same API; there are **no provider-specific endpoints** (the
LLM, embeddings, Confluence, and Jira providers are chosen by *config*, not by URL).
The only platform-coupled route is `POST /notify` (Webex).

## Authentication

Set `KAI_API_KEY` to require `Authorization: Bearer <key>` on every route **except
`GET /health`** (and `GET /`). With no key set (local dev) the API is open — KAI logs
a loud warning, because the whole corpus is then readable by anyone who can reach it.
When a key *is* set, the interactive docs (`/docs`, `/redoc`, `/openapi.json`) are also
hidden, so anonymous scanners can't enumerate the `/admin/*` surface. A missing or wrong
key returns **401**; the bearer comparison is constant-time.

## How a question flows (chat or API → answer)

A user typing in Webex/Slack/the web UI never hits a special endpoint — the chat
surface is a thin client that POSTs to **`/ask`**:

```
            ┌─ web UI (frontend/) ─┐
user types ─┼─ Webex bot ──────────┼─►  POST /ask  ─►  retrieve (hybrid: vector + keyword)
            └─ Slack bot ──────────┘                    └► rerank (cross-encoder)
                                                          └► confidence gate ──┐
                                                                               ├─ confident → grounded answer
                                                          grounding + verify ──┤   + citations
                                                          + numeric guards     └─ not sure → ESCALATE
                                                                                   (ticket / closest pages,
                                                                                    never a fabricated answer)
```

- The **web UI** calls `/ask` over CORS and renders the answer, sources, confidence,
  and an answered/escalated pill.
- The **Webex/Slack bots** (`kai/chat/`, run as separate processes) post the user's
  message to `/ask`, then post the reply back — Webex edits its "searching…" message
  in place. See [chat-platforms.md](chat-platforms.md) and
  [integrations-setup.md](integrations-setup.md).
- Same brain for all of them: one `/ask`, one pipeline, one never-fabricate guarantee.

### Which endpoint the bot calls

The **user never picks an endpoint** — it's routed by what's *in* the message. The
**Webex & Slack bots** share one `ChatService` (`kai/chat/service.py`); the **web UI**
calls `/ask` directly from the browser (text only):

| What the user does | Endpoint called | Webex | Slack | Web UI |
| --- | --- | :-: | :-: | :-: |
| Sends a question (text / @mention) | `POST /ask` | ✅ | ✅ | ✅ |
| Question **with a file attached** | `POST /ask-document` (ad-hoc RAG) | ✅ | — | ✅ |
| Taps 👍 / 👎 on the answer card | `POST /feedback` | ✅ | ✅ | — |
| Taps **Escalate** on the card | `POST /escalate` | ✅ | ✅ | — |

File-attachment Q&A works on **Webex** (drop a file + @mention) and the **web UI**
(the 📎 button) — both route to `/ask-document`, which answers from that file only and
never stores it. **Slack/Teams have no inbound-file handling yet** — a file there is
ignored and only the text is answered. The web UI has no feedback card (👍/👎) yet.
`/ingest`, `/admin/*`, and `/notify` are operator/outbound actions — never triggered
by a user message. Typing **`help`** (or `?`, `what can you do`) in any chat surface
shows a capabilities message instead of being answered as a question.

## Endpoints

17 routes. Auth = ✅ requires the bearer key (when `KAI_API_KEY` is set), 🔓 = always open.

### Core Q&A

| Method & path | Auth | Purpose |
| --- | --- | --- |
| `POST /ask` | ✅ | Answer a question from the corpus. Body `{question}`. Returns `{answer, citations[], confidence, escalated, escalation_url, suggested_sources[]}`. The one endpoint every chat surface uses. |
| `POST /ask-document` | ✅ | Ad-hoc Q&A over an **uploaded file** (no corpus writes). Body `{question, filename, content_b64}`. Same never-fabricate guards, scoped to that file. |
| `POST /search` | ✅ | Retrieve-only (no answer **generation**): top chunks + scores + the confidence/escalate decision. Uses the SAME retrieval path as `/ask`, so with `MULTI_QUERY`/`QUERY_REWRITE` on it still makes those query-expansion LLM calls; it just skips answer generation + verification. For evaluation/debugging. Body `{question}`. |

### Knowledge base

| Method & path | Auth | Purpose |
| --- | --- | --- |
| `POST /ingest` | ✅ | (Re)ingest the configured sources (Confluence instances + file dirs). Incremental — unchanged docs are skipped. Returns `{ingested}` (chunk count). |
| `POST /admin/reindex` | ✅ | Rebuild the vector index **in place** (re-embed every source + re-index approved curated answers), keeping the Inform queue / feedback / telemetry. Use after changing the embedding model/chunking. Returns `{chunks, curated}`. (A dimension change is refused — use `reset-db`.) |

### Health & observability

| Method & path | Auth | Purpose |
| --- | --- | --- |
| `GET /health` | 🔓 | Liveness — `{status:"ok"}`. Use for load-balancer / container checks. |
| `GET /` | 🔓 | JSON banner (name, links). The web UI is a **separate** app (`frontend/`), not served here. |
| `GET /metrics` | ✅ | Prometheus text counters (asks, escalations, latency). Reset on restart. |
| `GET /admin/gaps` | ✅ | Most-escalated questions — your content-gap backlog. |

### Feedback & escalation

| Method & path | Auth | Purpose |
| --- | --- | --- |
| `POST /feedback` | ✅ | 👍/👎 on an answer. Body `{question, verdict, reporter}`. A 👎 auto-quarantines a curated answer. |
| `POST /escalate` | ✅ | Human escalation from a chat surface — files a ticket WITHOUT re-running `/ask`. Body `{question, reporter}`. Returns `{status, escalation_url}` (`escalation_url` is `null` when no tracker is configured). |
| `POST /notify` | ✅ | **Webex-only** — proactively DM a user. Body `{email, message}`. Requires `WEBEX_BOT_TOKEN` (400 if unset); the recipient must have messaged the bot first (else **502**). |

### Inform loop (approval-gated learning)

| Method & path | Auth | Purpose |
| --- | --- | --- |
| `POST /admin/inform` | ✅ | Submit a human answer for a gap. Body `{question, answer, author, asker}`. |
| `GET /admin/inform` | ✅ | List candidates. Query: `status` (default `pending`; or `approved`/`rejected`/`revoked`/`quarantined`/`all`), `limit` (≤500), `offset`. |
| `POST /admin/inform/{id}/approve` | ✅ | Approve → synthesize into the curated KB. Body `{approver}` (optional 4-eyes: approver ≠ author). |
| `POST /admin/inform/{id}/reject` | ✅ | Reject a candidate. |
| `POST /admin/inform/{id}/revoke` | ✅ | Pull a previously-approved curated answer immediately. |

See [architecture.md](architecture.md) for the pipeline internals and
[chat-platforms.md](chat-platforms.md) for the chat layer.

> When KAI starts **without** a configured `.env`, the app still imports and serves a
> minimal fallback that returns `503` with a clear "not configured" message on every
> route (so tooling/CI can import it) — see the import-safety note in the changelog.

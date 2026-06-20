# KAI — a knowledge assistant that never guesses

> ### *Ask your docs. Get a cited answer — or an honest "I don't know."*

**KAI — Know · Ask · Inform.**
- **K**now — it grounds itself in *your* documents (the knowledge base it indexes).
- **A**sk — ask in plain language, from the web UI, the HTTP API, or a chat bot.
- **I**nform — it replies with **cited** sources; and via the *Inform loop* it can learn human-approved answers over time.

**What the tagline means:** take *"never guesses"* literally. **KAI** is a
self-hosted knowledge assistant for your team's documentation, and its one promise is
that it will **not make things up**. Every confident answer is drawn from — and
**cites** — your own documents; when those documents don't support an answer, KAI
**says so and escalates to a human** instead of inventing one. "No hallucinations" is
not a feature here, it's *the* design rule the whole system is built and tested around.

KAI answers questions about **your** documentation — Confluence spaces, PDFs, and
text/markdown files — and runs **fully self-hosted** (Postgres/pgvector plus **any
OpenAI-compatible LLM + embedding endpoint** — Ollama, vLLM, llama.cpp, or a hosted
API), so your content can stay entirely on your infrastructure.

> **The one rule:** never be confidently wrong. An out-of-scope or unanswerable
> question is *escalated*, not fabricated — enforced by layered grounding guards and
> validated by a golden question set (`eval/`) plus the automated test suite.

## Why KAI

- **Grounded answers with sources.** Every confident answer cites the pages it came
  from; the model is constrained to the retrieved context.
- **Refuses to guess.** A multi-layer gate (retrieval confidence + grounding checks
  + a verification pass) escalates anything it can't support — to a tracker ticket
  or a human — rather than inventing an answer.
- **Runs fully offline.** Point it at a local LLM (Ollama, vLLM, llama.cpp) and
  **nothing leaves your network** — not your questions, not your documents. No
  third-party model API, no telemetry; air-gap friendly. Every dependency sits
  behind a small swappable interface, so models/sources change by config, not code.
- **Meets people where they work.** A standalone web chat UI (in `frontend/`) with
  file upload, plus Webex, Slack, and Microsoft Teams bots — all the same brain behind
  one API. (Teams is included; verify it in your own Azure tenant.)
- **Learns, safely.** Questions it couldn't answer become a review queue; an
  approved human answer is curated into the knowledge base — with an audit trail, an
  optional second-approver requirement, and 👎-driven auto-removal of bad entries.

## Who it's for

Anywhere people repeatedly dig the same answers out of documents:

- **Teams tired of answering the same questions.** Point KAI at your wiki/Confluence and
  let it field the FAQs from Slack, Webex, or Teams — with citations, and an honest
  escalation when it doesn't know, so it never invents an answer.
- **A personal knowledge base.** Run it locally over your own notes, PDFs, and docs.
- **Studying / interview prep.** Load a syllabus, papers, or docs and quiz yourself —
  every answer is grounded in the source and cited, so you can trust *and* verify it.
- **"Just the relevant bit" of a big document.** Drop a long PDF/spec into the web UI
  (📎) and ask one pointed question instead of reading the whole thing.

## How it works

```
question → retrieve (hybrid: vector + full-text, RRF) → rerank → confidence gate
        → grounded answer with citations   ── or ──   escalate (never fabricate)
```

Sources (Confluence / files) → chunk → embed → Postgres + pgvector. Retrieval fuses
dense and full-text search (RRF), a cross-encoder reranks the candidate pool (on in
the shipped config), and a confidence gate plus grounding/verification guards decide
answer-vs-escalate **before** anything reaches the user.

> **Built simply, on purpose.** KAI implements the RAG pipeline directly — **no
> LangChain or LangGraph** — so it stays small, readable, and easy to self-host. The
> same design extends cleanly to LangChain/LangGraph if you later want agentic or more
> advanced features. Tested with **`qwen2.5:14b-instruct`** (LLM) and
> **`nomic-embed-text`** (embeddings, 768-dim) via Ollama; any OpenAI-compatible
> endpoint works.

## Quick start — easy setup with `run/setup.py`

One script handles the whole setup (venv, dependencies, `.env`, database) — no Docker
required. Prereqs: Python 3.12+, PostgreSQL with the `pgvector` extension, and
[Ollama](https://ollama.com) serving a local LLM + embedding model
(`ollama pull qwen2.5:14b-instruct && ollama pull nomic-embed-text`).

Runs on **Linux and macOS** (CI on Ubuntu, developed on macOS). On **Windows**, use
WSL or Docker, or run `uvicorn kai.app:app` directly — the `run/setup.py` helper
assumes a Unix shell plus the Postgres CLI tools (`psql`/`createdb`).

```bash
python run/setup.py install        # venv + deps + creates .env + database
#   then edit .env  (LLM / DB / Confluence)
python run/setup.py start          # start the API on :8100
python run/setup.py ingest         # build the knowledge index
python run/setup.py ui             # open the web chat UI on :3000
```

### Everyday commands (and which one to use)

| Command | What it does | Touches existing data? |
| --- | --- | --- |
| `setup.py ingest` | Add new / changed docs (incremental — unchanged skipped) | No — safe to run anytime |
| `setup.py reindex` | Re-embed everything **in place** (after a chunking change) | Keeps curated answers, feedback, telemetry |
| `setup.py reset-db` | Drop the **entire** database | ⚠️ Deletes everything |
| `setup.py fresh` | Clean-slate bring-up: **reset-db** → install → start → ingest | ⚠️ Wipes the DB first |

Day-to-day you want **`ingest`**. Use **`reindex`** only after changing the embedding
model or chunking. **`fresh`/`reset-db`** are destructive — for a brand-new machine or
a deliberate wipe.

The API is now live. To chat in a browser, serve the separate web UI (it calls the
API over HTTP — see [frontend/](frontend/)):

```bash
python -m http.server 3000 -d frontend     # then open http://localhost:3000
```

Or call the API directly:

```bash
curl -s -X POST http://127.0.0.1:8100/ask \
  -H 'Content-Type: application/json' -d '{"question":"How does replication work?"}'
```

`python run/setup.py doctor` checks Postgres / pgvector / Ollama / deps.

**Other ways to run** (all in [doc/deploy.md](doc/deploy.md)) — simplest first:
1. **`run/setup.py`** (above) — the simplest path, and the one exercised by tests; works the same on a laptop or a server.
2. **Manual** — `pip install ".[bot,slack,rerank]"` then `uvicorn kai.app:app --port 8100`. (The `rerank` extra pulls `sentence-transformers`, which the shipped `.env.example` needs for its `RERANKER=cross_encoder` default; omit it only if you set `RERANKER=noop`.)
3. **Docker Compose** — `docker compose up` for a turnkey API + Postgres + web UI.

## Chat integrations

The chat bot is a thin client over the `/ask` API; pick one with `CHAT_PLATFORM`.
Full setup (creating the bot, getting tokens, configuring) is in
**[doc/integrations-setup.md](doc/integrations-setup.md)**.

| Platform | Public URL needed | Status |
| --- | --- | --- |
| Web UI (`frontend/`) | no | ready |
| Webex | no (websocket) | ready |
| Slack | no (Socket Mode) | ready |
| Microsoft Teams | yes (Azure Bot) | shipped — verify in your Azure tenant |

```bash
CHAT_PLATFORM=webex python run/setup.py bot      # or: CHAT_PLATFORM=slack | teams
```

In any chat, type **`help`** to see what KAI can do. **Attach a file** (PDF / text /
Markdown / HTML) on **Webex** or the **web UI** (📎) to ask about *that file only* —
it's read once for your question and **never stored** in the knowledge base. Replies
stay in-thread, so several people can use the bot in one space without crossing wires.

## HTTP API

| Method & path | Purpose |
| --- | --- |
| `GET /health` | liveness |
| `POST /ask` | answer a question → answer, citations, confidence, escalated |
| `POST /ask-document` | ad-hoc Q&A over an uploaded file (read once, **never stored**) |
| `POST /ingest` | (re)build the index — incremental (unchanged docs skipped) |
| `POST /admin/reindex` | rebuild the vector index **in place** (keeps curated answers/feedback) |
| `POST /search` | retrieve-only (no LLM) — for evaluation/debugging |
| `POST /feedback`, `/escalate` | 👍/👎 + human escalation from the chat surface |
| `POST /notify` | proactively DM a user (Webex-only; needs `WEBEX_BOT_TOKEN`) |
| `GET /metrics`, `/admin/gaps` | observability + most-escalated questions |
| `POST /admin/inform[...]` | curate / approve / revoke learned answers |

Set `KAI_API_KEY` to require a bearer token on everything except `/health`. **Full
endpoint reference + the chat→answer flow:** [doc/api.md](doc/api.md).

## Configuration

All configuration is environment variables (see `.env.example`). Essentials: the
LLM + embeddings endpoints (`LLM_*`, `EMBED_*`), the database (`DATABASE_URL`), the
knowledge source (`CONFLUENCE_*` and/or `SOURCE_TYPE`/`SOURCE_DIR`), and the tuned
retrieval settings shipped in `.env.example` (`RERANKER=cross_encoder`,
`MULTI_QUERY=true`, `CONFIDENCE_THRESHOLD=0.45` — the in-code field defaults are
`noop`/`false`/`0.45`). `EMBED_DIMENSIONS` must match the embedding model's width
(changing the model needs a re-ingest).

## Documentation

- [doc/faq.md](doc/faq.md) — **FAQ**: setup, config, LLM, accuracy, settings, bots/sockets, usage patterns, gotchas
- [doc/api.md](doc/api.md) — every endpoint, what it's for, and how a question flows
- [doc/deploy.md](doc/deploy.md) — deploy with Docker Compose or systemd (+ provider matrix)
- [doc/setup-and-run.md](doc/setup-and-run.md) — install & run, in depth
- [doc/integrations-setup.md](doc/integrations-setup.md) — Webex / Slack / Teams setup + tokens
- [doc/sources.md](doc/sources.md) — knowledge sources (Confluence, files/PDF)
- [doc/chat-platforms.md](doc/chat-platforms.md) — chat architecture + adding a platform
- [doc/architecture.md](doc/architecture.md) — system architecture
- [doc/roadmap.md](doc/roadmap.md) — what's done + planned (more sources, ticketing, paging)
- [CHANGELOG.md](CHANGELOG.md)

## Testing

```bash
pytest                          # unit + integration suite (no Ollama needed)
python eval/run_eval.py         # live accuracy gate against a golden question set
python eval/simulate_bot.py     # dry-run the chat bots without platform tokens
```

## Limitations & roadmap

KAI is a complete, working knowledge assistant, kept deliberately small. Known limits
of the current implementation:

- **Access control is per-platform.** Only **Webex** has a per-user allowlist;
  Slack/Teams answer anyone who can reach the bot, so gate them at the workspace/tenant
  level. (A shared allowlist is on the roadmap.)
- **Teams needs a public HTTPS URL + an Azure bot** — it can't be exercised purely
  locally like Webex/Slack (which use outbound websockets).
- **Sources are Confluence + local files** today. Web/URL crawler, SharePoint, Google
  Drive, and Git connectors are planned.
- **Escalation tracker is Jira** (plus a local fallback). Other trackers (GitHub/GitLab
  issues, ServiceNow, email) are planned.
- **No token streaming** — the grounding/verification guards run *after* generation, so
  KAI shows an edit-in-place "Searching… → answer" ack instead of streaming tokens a
  guard might later retract.
- **One embedding model/dimension at a time** — changing it requires a re-ingest.
- **Tuned for small-to-medium corpora.** Very large ones may need retrieval tuning
  (e.g. `halfvec`, `TOP_K`, chunk sizes).
- **Quality depends on your LLM/embeddings.** Tested with `qwen2.5:14b-instruct` +
  `nomic-embed-text`; smaller models follow grounding less reliably.
- **Framework-free by design** (no LangChain/LangGraph) — agentic/advanced features
  would extend the existing provider interfaces, not replace them.

Full planned work is in [doc/roadmap.md](doc/roadmap.md).

## Author & license

Created by **Rahul Mahadik** — [rahulmahadik.com](https://rahulmahadik.com) ·
[technoscripts.com](https://technoscripts.com).
Released under the [MIT License](LICENSE).

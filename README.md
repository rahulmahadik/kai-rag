# KAI, a knowledge assistant that never guesses

[![CI](https://github.com/rahulmahadik/kai-rag/actions/workflows/ci.yml/badge.svg)](https://github.com/rahulmahadik/kai-rag/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)
[![Code style: Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
![Version 1.1.0](https://img.shields.io/badge/version-1.1.0-success.svg)

> ### Ask your docs. Get a cited answer, or an honest "I don't know."

![KAI answering the same question on the web UI, in Slack, and in Webex, grounded and cited](doc/screenshots/hero.png)

**KAI: Know, Ask, Inform.**

- **Know**: it grounds itself in *your* documents, the knowledge base it indexes.
- **Ask**: ask in plain language from the web UI, the HTTP API, or a chat bot.
- **Inform**: it replies with cited sources, and through the Inform loop it can
  learn human-approved answers over time.

KAI is a self-hosted knowledge assistant for your team's documentation. Its one
promise is that it will not make things up. Every confident answer is drawn from,
and cites, your own documents. When those documents don't support an answer, KAI
says so and escalates to a human instead of inventing one. "No hallucinations" is
not a feature here; it is the design rule the whole system is built and tested
around.

KAI answers questions about your documentation (Confluence spaces, PDFs, and
text/markdown files) and runs fully self-hosted on Postgres/pgvector plus any
OpenAI-compatible LLM and embedding endpoint: Ollama, vLLM, llama.cpp, or a hosted
API. Your content can stay entirely on your infrastructure.

> **The one rule:** never be confidently wrong. An out-of-scope or unanswerable
> question is escalated, not fabricated. Layered grounding guards enforce it, and a
> golden question set (`eval/`) plus the automated test suite validate it.

## Why KAI

- **Grounded answers with sources.** Every confident answer cites the pages it came
  from, and the model is constrained to the retrieved context.
- **Refuses to guess.** A multi-layer gate (retrieval confidence, grounding checks,
  and a verification pass) escalates anything it can't support to a tracker ticket
  or a human rather than inventing an answer.
- **Runs fully offline.** Point it at a local LLM (Ollama, vLLM, llama.cpp) and
  nothing leaves your network: not your questions, not your documents. No
  third-party model API, no telemetry, air-gap friendly. Every dependency sits
  behind a small swappable interface, so models and sources change by config rather
  than code.
- **Meets people where they work.** A standalone web chat UI (in `frontend/`) with
  file upload, plus Webex, Slack, and Microsoft Teams bots, all backed by one API.
  Teams is included; verify it in your own Azure tenant.
- **Learns, safely.** Questions it couldn't answer become a review queue. An
  approved human answer is curated into the knowledge base, with an audit trail, an
  optional second-approver requirement, and downvote-driven auto-removal of bad
  entries.

## Who it's for

Anywhere people repeatedly dig the same answers out of documents:

- **Teams tired of answering the same questions.** Point KAI at your wiki or
  Confluence and let it field the FAQs from Slack, Webex, or Teams, with citations
  and an honest escalation when it doesn't know.
- **A personal knowledge base.** Run it locally over your own notes, PDFs, and docs.
- **Studying and interview prep.** Load a syllabus, papers, or docs and quiz
  yourself. Every answer is grounded in the source and cited, so you can trust it
  and verify it.
- **The relevant bit of a big document.** Drop a long PDF or spec into the web UI
  and ask one pointed question instead of reading the whole thing.

## How it works

```
question -> retrieve (hybrid: vector + full-text, RRF) -> rerank -> confidence gate
         -> grounded answer with citations    or    escalate (never fabricate)
```

Sources (Confluence, files) are chunked, embedded, and stored in Postgres with
pgvector. Retrieval fuses dense and full-text search with RRF, a cross-encoder
reranks the candidate pool (on in the shipped config), and a confidence gate plus
grounding and verification guards decide answer-versus-escalate before anything
reaches the user.

> **Built simply, on purpose.** KAI implements the RAG pipeline directly, with no
> LangChain or LangGraph, so it stays small, readable, and easy to self-host. The
> same design extends cleanly to those frameworks if you later want agentic or more
> advanced features. Tested with `qwen2.5:14b-instruct` for generation and
> `nomic-embed-text` (768-dim) for embeddings via Ollama; any OpenAI-compatible
> endpoint works.

## Quick start with `run/setup.py`

One script handles the whole setup: venv, dependencies, `.env`, and database. No
Docker required. Prereqs: Python 3.12+, PostgreSQL with the `pgvector` extension,
and [Ollama](https://ollama.com) serving a local LLM and embedding model
(`ollama pull qwen2.5:14b-instruct && ollama pull nomic-embed-text`).

Runs on Linux and macOS (CI on Ubuntu, developed on macOS). On Windows, use WSL or
Docker, or run `uvicorn kai.app:app` directly: the `run/setup.py` helper assumes a
Unix shell plus the Postgres CLI tools (`psql`/`createdb`).

```bash
python run/setup.py install        # venv + deps + creates .env + database
#   then edit .env  (LLM / DB / Confluence)
python run/setup.py start          # start the API on :8100
python run/setup.py ingest         # build the knowledge index
python run/setup.py ui             # open the web chat UI on :3000
```

### Everyday commands, and which one to use

| Command | What it does | Touches existing data? |
| --- | --- | --- |
| `setup.py ingest` | Add new or changed docs (incremental, unchanged skipped) | No, safe to run anytime |
| `setup.py reindex` | Re-embed everything in place (after a chunking change) | Keeps curated answers, feedback, telemetry |
| `setup.py reset-db` | Drop the entire database | Deletes everything |
| `setup.py fresh` | Clean-slate bring-up: reset-db, install, start, ingest | Wipes the DB first |
| `setup.py stop` / `status` | Stop the background API, or check whether it is up | No |
| `setup.py doctor` | Check Postgres, pgvector, Ollama, and deps | No |

Day to day you want `ingest`. Use `reindex` only after changing the embedding model
or chunking. `fresh` and `reset-db` are destructive: for a brand-new machine or a
deliberate wipe.

The API is now live. To chat in a browser, serve the separate web UI, which calls
the API over HTTP (see [frontend/](frontend/)):

```bash
python -m http.server 3000 -d frontend     # then open http://localhost:3000
```

Or call the API directly:

```bash
curl -s -X POST http://127.0.0.1:8100/ask \
  -H 'Content-Type: application/json' -d '{"question":"How does replication work?"}'
```

`python run/setup.py doctor` checks Postgres, pgvector, Ollama, and deps.

**Other ways to run** (all in [doc/deploy.md](doc/deploy.md)), simplest first:

1. **`run/setup.py`** (above), the simplest path. Works the same on a laptop or a
   server. It is a convenience wrapper: the API itself is what the test suite and
   CI exercise.
2. **Manual**: `pip install ".[bot,slack,teams,rerank]"` then
   `uvicorn kai.app:app --port 8100`. Take only the extras you need: `bot` for
   Webex, `slack`, `teams`, and `rerank`. The `rerank` extra pulls
   `sentence-transformers`, which the shipped `.env.example` needs for its
   `RERANKER=cross_encoder` default; omit it only if you set `RERANKER=noop`.
3. **Docker Compose**: `cp .env.example .env`, fill in your LLM and embedding
   endpoints, then `docker compose up` for an API, Postgres, and web UI. Compose
   requires the `.env` file and sets `RERANKER=noop` by default, because the
   default image is built without the (large) `rerank` extra. To use the
   cross-encoder, add `rerank` to the `EXTRAS` build arg and set
   `RERANKER=cross_encoder` in the compose environment.

## Chat integrations

The chat bot is a thin client over the `/ask` API; pick one with `CHAT_PLATFORM`.
Full setup (creating the bot, getting tokens, configuring) is in
**[doc/integrations-setup.md](doc/integrations-setup.md)**.

| Platform | Public URL needed | Status |
| --- | --- | --- |
| Web UI (`frontend/`) | no | ready |
| Webex | no (websocket) | ready |
| Slack | no (Socket Mode) | ready |
| Microsoft Teams | yes (Azure Bot) | shipped, verify in your Azure tenant |

```bash
CHAT_PLATFORM=webex python run/setup.py bot      # or: CHAT_PLATFORM=slack | teams
```

In any chat, type `help` to see what KAI can do. Attach a file (PDF, text,
Markdown, or HTML) on Webex or the web UI to ask about that file only: it is read
once for your question and never stored in the knowledge base. Replies stay
in-thread, so several people can use the bot in one space without crossing wires.

## HTTP API

| Method and path | Purpose |
| --- | --- |
| `GET /health` | liveness |
| `POST /ask` | answer a question: answer, citations, confidence, escalated |
| `POST /ask-document` | ad-hoc Q&A over an uploaded file (read once, never stored) |
| `POST /ingest` | (re)build the index, incremental (unchanged docs skipped) |
| `POST /admin/reindex` | rebuild the vector index in place (keeps curated answers and feedback) |
| `POST /search` | retrieve-only: ranked chunks and scores, no answer generation |
| `POST /feedback`, `/escalate` | thumbs up/down plus human escalation from the chat surface |
| `POST /notify` | proactively DM a user (Webex only; needs `WEBEX_BOT_TOKEN`) |
| `GET /metrics`, `/admin/gaps` | observability and most-escalated questions |
| `POST /admin/inform[...]` | curate, approve, and revoke learned answers |

Set `KAI_API_KEY` to require a bearer token on every endpoint except `GET /health`
and the `GET /` banner, which stay open for liveness checks. Setting a key also
hides the interactive docs and `/openapi.json`. The full endpoint reference and the
chat-to-answer flow are in [doc/api.md](doc/api.md).

## Configuration

All configuration is environment variables (see `.env.example`). The essentials are
the LLM and embeddings endpoints (`LLM_*`, `EMBED_*`), the database
(`DATABASE_URL`), the knowledge source (`CONFLUENCE_*` and/or
`SOURCE_TYPE`/`SOURCE_DIR`), and the tuned retrieval settings shipped in
`.env.example` (`RERANKER=cross_encoder`, `MULTI_QUERY=true`,
`CONFIDENCE_THRESHOLD=0.45`; the in-code field defaults are `noop`, `false`, and
`0.45`). `EMBED_DIMENSIONS` must match the embedding model's width, and changing the
model needs a re-ingest.

The web UI is served from a different origin than the API, so it needs
`CORS_ORIGINS` set to its exact URL (for example
`CORS_ORIGINS=http://localhost:3000`). It is deny-by-default: with `CORS_ORIGINS`
unset, no browser origin can call the API and the UI will fail to reach it.

## Documentation

- [doc/faq.md](doc/faq.md): setup, config, LLM, accuracy, settings, bots and
  sockets, usage patterns, gotchas
- [doc/api.md](doc/api.md): every endpoint, what it's for, and how a question flows
- [doc/deploy.md](doc/deploy.md): deploy with Docker Compose or systemd, plus a
  provider matrix
- [doc/setup-and-run.md](doc/setup-and-run.md): install and run, in depth
- [doc/integrations-setup.md](doc/integrations-setup.md): Webex, Slack, and Teams
  setup and tokens
- [doc/sources.md](doc/sources.md): knowledge sources (Confluence, files, PDF)
- [doc/chat-platforms.md](doc/chat-platforms.md): chat architecture and adding a
  platform
- [doc/architecture.md](doc/architecture.md): system architecture
- [doc/roadmap.md](doc/roadmap.md): what's done and planned
- [doc/requirements.md](doc/requirements.md): the design decisions behind the build
- [CHANGELOG.md](CHANGELOG.md), [CONTRIBUTING.md](CONTRIBUTING.md),
  [SECURITY.md](SECURITY.md)

## Testing

Install the test dependencies first (`run/setup.py install` installs the runtime
deps only):

```bash
pip install -e ".[dev,teams,uitest]"
python -m playwright install chromium   # only for the web-UI suite
```

```bash
pytest                          # unit suite, no services needed
ruff check . && ruff format --check .
python eval/run_eval.py         # live accuracy gate against a golden question set
python eval/simulate_bot.py     # dry-run the chat bots without platform tokens
```

Three further suites exercise the same code against real services. Each is skipped
unless you point it at what it needs, so a plain `pytest` stays fast and offline:

| Suite | Needs | How to run |
| --- | --- | --- |
| `tests/integration/` (store, pipeline, telemetry) | Postgres + pgvector | `KAI_TEST_DATABASE_URL=postgresql://kai:kai@localhost:5432/kai_test pytest -q` |
| `tests/integration/test_live_llm.py` | the above, plus an LLM | add `KAI_TEST_LLM_BASE_URL=http://localhost:11434/v1` |
| `tests/integration/test_web_ui.py` | Playwright + Chromium | `pytest -q tests/integration/test_web_ui.py` |

The web-UI suite loads `frontend/index.html` in a real browser and asserts on the
rendered DOM, including that answer markup is escaped and that a citation URL with
a non-navigating scheme never becomes a link.

## Limitations and roadmap

KAI is a complete, working knowledge assistant, kept deliberately small. Known
limits of the current implementation:

- **Access control is per-platform.** Only Webex has a per-user allowlist. Slack and
  Teams answer anyone who can reach the bot, so gate them at the workspace or tenant
  level. A shared allowlist is on the roadmap.
- **Teams needs a public HTTPS URL and an Azure bot,** so it can't be exercised
  purely locally like Webex and Slack, which use outbound websockets.
- **Sources are Confluence and local files** today. Web crawler, SharePoint, Google
  Drive, and Git connectors are planned.
- **The escalation tracker is Jira,** plus a local fallback. Other trackers
  (GitHub/GitLab issues, ServiceNow, email) are planned.
- **No token streaming.** The grounding and verification guards run after
  generation, so KAI shows an edit-in-place "searching, then answer" acknowledgement
  instead of streaming tokens a guard might later retract.
- **One embedding model and dimension at a time.** Changing it requires a re-ingest.
- **Tuned for small-to-medium corpora.** Very large ones may need retrieval tuning
  (`halfvec`, `TOP_K`, chunk sizes).
- **Quality depends on your LLM and embeddings.** Tested with
  `qwen2.5:14b-instruct` and `nomic-embed-text`; smaller models follow grounding
  less reliably.
- **Framework-free by design.** Agentic or advanced features would extend the
  existing provider interfaces rather than replace them.

Full planned work is in [doc/roadmap.md](doc/roadmap.md).

## Author and license

Created by Rahul Mahadik: [rahulmahadik.com](https://rahulmahadik.com),
[technoscripts.com](https://technoscripts.com).
Released under the [MIT License](LICENSE).

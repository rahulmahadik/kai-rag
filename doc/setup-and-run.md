# KAI — Setup & Run Guide

**Author:** Rahul Mahadik

KAI is an enterprise knowledge assistant. It answers questions from your
knowledge base (Confluence) with **grounded, cited answers**, and — critically —
it **escalates instead of guessing** when it isn't confident, so it never hands a
user a confident wrong answer. It runs fully on your own machine (local LLM,
embeddings, reranker, and vector database), so your content stays on your
infrastructure.

**What to expect** (validated by the golden question set in `eval/` and the
automated test suite):

- Out-of-scope or unsupported questions are **escalated, never fabricated**.
- Retrieval is robust to typos and casual phrasing (hybrid search + cross-encoder
  rerank + optional multi-query).
- Answers carry clickable source links; low-confidence questions open a ticket.

Run the accuracy gate against your own corpus with `python eval/run_eval.py`.

You can use KAI three ways: a **web chat UI** (the separate `frontend/` app — open
it in a browser), the **HTTP API** (`POST /ask`), or a **chat bot** in Webex/Slack
(`@KAI <question>`). The chat-bot setup is in [integrations-setup.md](integrations-setup.md).

---

## What you need (one-time)

| Requirement | Notes |
|---|---|
| **Python 3.12+** | `python3 --version` |
| **PostgreSQL 14+ with the `pgvector` extension** | Local install is fine. `setup.py doctor` checks it. |
| **[Ollama](https://ollama.com)** running locally | Serves the LLM + embeddings on `:11434`. |
| Two Ollama models | `ollama pull qwen2.5:14b-instruct` and `ollama pull nomic-embed-text` |
| _(optional)_ A Webex/Slack account | Only if you want the **chat bot** — not needed for the web UI or the HTTP API. |

> The cross-encoder reranker model (~90 MB) downloads automatically the first time the
> API starts (one-time) — no manual step. It loads in a background thread, so `/health`
> stays responsive while it downloads.

---

## Step 1 — Install

From the repository root:

```bash
python run/setup.py install     # creates .venv, installs deps, ensures the 'kai' Postgres DB
python run/setup.py doctor      # confirms Postgres / pgvector / Ollama / deps are all green
```

`doctor` should print all green checks before you continue.

---

## Step 2 — Configure

```bash
cp .env.example .env
```

**You don't need to change anything to start.** `.env` ships pointing at the
public **Apache `COMDEV`** Confluence space with the tuned settings, so it works
out of the box — the **only value you'll add is your Webex bot token** (Step 5).

Everything else is optional, for reference:

| Setting | Default | When you'd change it |
|---|---|---|
| `CONFLUENCE_*` | public `COMDEV` | Later, to point at your own Confluence (see the end of this guide). |
| `LLM_MODEL` / `EMBED_MODEL` | `qwen2.5:14b-instruct` / `nomic-embed-text` | To swap models — no code change. |
| `CONFLUENCE_MAX_DOCS` | `40` | Cap pages while testing; `0` = whole space. |
| `CONFIDENCE_THRESHOLD` / `RERANKER` | `0.45` / `cross_encoder` | Tuned — leave as-is. |

Secrets live only in `.env`, which is git-ignored and never committed.

---

## Step 3 — Load the knowledge base

```bash
python run/setup.py start       # starts the API on http://127.0.0.1:8100
python run/setup.py ingest      # fetches + indexes the configured Confluence space
```

`ingest` prints how many chunks were written (e.g. `{"ingested": 308}`). Re-run
it whenever the source content changes.

---

## Step 4 — Verify the API (optional but recommended)

```bash
# A question the knowledge base CAN answer → grounded answer + a source link:
curl -s -XPOST http://127.0.0.1:8100/ask -H 'Content-Type: application/json' \
  -d '{"question":"What does the mentoring programme expect of mentees?"}'

# A question it CANNOT answer → escalated, with a ticket link (never a wrong answer):
curl -s -XPOST http://127.0.0.1:8100/ask -H 'Content-Type: application/json' \
  -d '{"question":"How do I reset my VPN password?"}'
```

OpenAPI docs are at `http://127.0.0.1:8100/docs` in local mode. (They are intentionally
disabled once `KAI_API_KEY` is set, so a keyed deployment doesn't expose its schema or
the `/admin/*` surface to anonymous scanners.)

---

## Step 5 — Create the Webex bot & get its token

1. Sign in at **<https://developer.webex.com>**.
2. Open **My Webex Apps → Create a New App → Create a Bot**.
3. Give it a name and username (e.g. `kai`), pick an icon, and create it.
4. **Copy the bot access token** shown once on the confirmation screen.
5. Paste it into `.env`:

   ```bash
   WEBEX_BOT_TOKEN=<paste-the-bot-access-token>
   ```

Note the bot's email address (e.g. `kai@webex.bot`) — you'll use it to add the
bot to a space.

---

## Step 6 — Run the bot

With the API already running (Step 3):

```bash
python run/setup.py bot
```

This opens an **outbound websocket** to Webex. There is **no public URL, no
tunnel, and no firewall change** — your machine only needs normal outbound
internet. Leave it running (Ctrl-C to stop).

---

## Step 7 — Use it in Webex

1. In Webex, start a space (or use a direct message) and **add the bot** by its
   email (`kai@webex.bot`).
2. In a **group space**, mention it: `@KAI What is the mentoring programme?`
   In a **1:1** space, just type your question.
3. KAI replies with a grounded answer and its sources, and escalates with a
   ticket link when it isn't confident.

### Office (non-personal) Webex

The code and the bot token are **identical** for a personal and an organization
account — the only difference is your organization's bot policy in Webex Control
Hub. If custom bots are restricted there, ask your Webex administrator to
allow-list this bot by its email; otherwise it works immediately.

---

## Pointing KAI at your private Confluence (later)

To answer from your own (private) Confluence space instead of the public test
space, set these in `.env` and re-ingest:

```bash
CONFLUENCE_BASE_URL=https://your-domain.atlassian.net/wiki
CONFLUENCE_EMAIL=you@your-company.com
CONFLUENCE_API_TOKEN=<your-confluence-api-token>   # id.atlassian.com → API tokens
CONFLUENCE_SPACE_KEY=ENG
CONFLUENCE_MAX_DOCS=0
# Optional — index just ONE page + all its child/descendant pages (a subtree)
# instead of the whole space. Accepts a page id or an exact page title:
# CONFLUENCE_ROOT_PAGE=Engineering Handbook
```

> **Scope:** by default KAI indexes the **whole space** (`CONFLUENCE_SPACE_KEY`).
> Set `CONFLUENCE_ROOT_PAGE` to a page (id or exact title) to index **only that
> page and everything beneath it** — handy for pointing KAI at one section of a
> large corporate space.

```bash
python run/setup.py reset-db    # clear the public test index
python run/setup.py restart
python run/setup.py ingest      # index your private space
```

The connector switches to authenticated access automatically when email + token
are present. The token is used only to read your Confluence and never leaves
your machine.

---

## Everyday commands

```bash
python run/setup.py status      # is the API up? + /health
python run/setup.py restart     # restart the API (after a config change)
python run/setup.py ingest      # refresh the index when content changes (incremental)
python run/setup.py reindex     # rebuild the vector index in place (keeps curated/feedback)
python run/setup.py bot         # run the chat bot (CHAT_PLATFORM=webex|slack|teams)
python run/setup.py stop        # stop the API
python run/setup.py doctor      # environment health check
python run/setup.py reset-db    # ⚠️ drop + recreate the DB (wipes everything)
```

Use **`ingest`** day-to-day; **`reindex`** only after changing the embedding model or
chunking; **`reset-db`**/**`fresh`** are destructive (clean-slate rebuild).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `doctor` flags Postgres/pgvector | Ensure Postgres is running and the `pgvector` extension is installed. |
| `doctor` flags Ollama | `ollama serve` and confirm the two models are pulled. |
| Bot exits asking for a token | Set `WEBEX_BOT_TOKEN` in `.env`. |
| Bot can't reach the API | Run `python run/setup.py start` first; check `KAI_API_URL`. |
| Answers look stale | Re-run `python run/setup.py ingest` after the source changes. |
| Web UI can't reach the API (CORS error in the browser console) | Add the UI's exact origin to `CORS_ORIGINS` in `.env` and restart — it's deny-by-default. The shipped `.env.example` already allows `http://localhost:3000` and `:5173`. |
| Bot doesn't respond in an office space | Your org may restrict bots — have an admin allow-list the bot's email. |

---

## How it works (one paragraph)

A question is embedded and matched against the indexed Confluence content using
hybrid search (semantic + keyword), the top candidates are re-ranked by a
cross-encoder for precision, and a confidence score decides the outcome: if the
question is well-covered, the local model writes a grounded, cited answer; if it
isn't, KAI escalates by opening a ticket rather than guessing. The model,
embeddings, reranker, and vector database all run locally; the chat transport is
Webex.

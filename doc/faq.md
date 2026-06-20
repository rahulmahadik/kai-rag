# KAI FAQ

Common questions and points of confusion — grouped by setup, config, LLM, accuracy,
settings, bots/sockets, usage patterns, and gotchas. For step-by-step bot setup see
[integrations-setup.md](integrations-setup.md); for architecture see
[architecture.md](architecture.md).

## Setup & install

**What do I actually need to run KAI?**
Python 3.12+, PostgreSQL with the `pgvector` extension, and any **OpenAI-compatible**
LLM + embedding endpoint (e.g. [Ollama](https://ollama.com) locally, vLLM, llama.cpp,
or a hosted API). No Docker required — `python run/setup.py install` sets up a venv,
deps, `.env`, and the database.

**Do I need Docker?** No. `run/setup.py` is the simplest path. Docker Compose is
provided as an alternative (`docker compose up` → API + Postgres + web UI).

**Windows?** Use WSL or Docker, or run `uvicorn kai.app:app` directly — the
`run/setup.py` helper assumes a Unix shell plus `psql`/`createdb`.

**How do I load my own documents?** Set `SOURCE_TYPE` (`confluence`, `files`, or
`confluence+files`) and `SOURCE_DIR`/`CONFLUENCE_*`, then `python run/setup.py ingest`
(or `POST /ingest`). See [sources.md](sources.md).

**I added new docs — do I re-ingest everything?** No. `ingest` is **incremental**
(unchanged docs are skipped). Use `reindex` only when you change the embedding model
or chunking — it re-embeds **in place** and keeps curated answers/feedback/telemetry.

## Config & settings

**Where is configuration?** All environment variables — copy `.env.example` to `.env`
and edit. Every field has an inline comment.

**The settings that matter most:**
- `LLM_*` and `EMBED_*` — your model endpoints, keys, and model names.
- `DATABASE_URL` — Postgres/pgvector connection.
- `SOURCE_TYPE` / `SOURCE_DIR` / `CONFLUENCE_*` — where knowledge comes from.
- `CONFIDENCE_THRESHOLD` (default `0.45`) — the answer-vs-escalate gate (below).
- `RERANKER` (`cross_encoder` in the shipped `.env.example`, `noop` in code default),
  `MULTI_QUERY`, `TOP_K` — retrieval quality knobs.
- `EMBED_DIMENSIONS` — **must** match the embedding model's width (768 for
  `nomic-embed-text`). Changing the embedding model needs a **re-ingest**.

**What is `CONFIDENCE_THRESHOLD`?** A calibrated gate: if retrieval/answer confidence
is below it, KAI escalates instead of answering. Raise it to be stricter (escalate
more, answer only when very sure); lower it to answer more (at higher hallucination
risk). Tune against your own corpus with the golden eval in [`eval/`](../eval/).

## LLM & embeddings

**Which models can I use?** Any OpenAI-compatible chat + embeddings endpoint. KAI is
**tested with `qwen2.5:14b-instruct`** (LLM) and **`nomic-embed-text`** (embeddings,
768-dim) via Ollama.

**Can I use OpenAI / a hosted API?** Yes — point `LLM_BASE_URL` / `LLM_API_KEY` (and
`EMBED_*`) at it. **Can I run fully offline?** Yes — with Ollama/local models nothing
leaves your network.

**Does the model choice affect accuracy?** Yes. A capable **instruct** model follows
the grounding instructions better (sticks to retrieved context, says "I don't know").
Very small models may escalate more or format citations less reliably.

**Why is the first answer slow?** A cold local model loads on first call (tens of
seconds); subsequent answers are faster, and identical questions are cached. The chat
bots post an instant "🔎 Searching…" ack so it doesn't look frozen.

## Accuracy & the never-fabricate guarantee

**How does KAI avoid hallucinations?** Layered: hybrid retrieval (dense + full-text,
RRF) → cross-encoder rerank → a confidence gate → grounding checks → an optional LLM
verification pass (`VERIFY_ANSWERS`, on by default, fails open). Anything unsupported
is **escalated**, not invented. Validated by the golden eval + the test suite.

**KAI escalated a question it *should* know — why?** The supporting page probably
wasn't retrieved or scored below the threshold. Fixes: make sure the doc is ingested,
lower `CONFIDENCE_THRESHOLD` a little, raise `TOP_K`, or improve chunking. Use
`POST /search` to see exactly what was retrieved and the escalate decision.

**KAI gave a wrong/incomplete answer — what do I do?** Use 👎 or **Escalate** in the
chat card; that signals a gap. Through the **Inform loop** an approved human answer can
be curated into the knowledge base (with an audit trail and 👎-driven auto-removal).

**Is escalation an error?** No — it's the product working. "I couldn't answer this
confidently… flagged it for a human" is the never-fabricate guarantee, not a failure.

**How do I tune accuracy?** A few levers, roughly in order of impact:

1. **Make the content retrievable first.** Confirm the doc is ingested, and use
   `POST /search` to see exactly what comes back for a query (and the escalate decision)
   before changing anything else.
2. **Retrieval breadth & ranking.** Raise `TOP_K` (more candidates), keep
   `RERANKER=cross_encoder` on, and enable `MULTI_QUERY=true` / `QUERY_REWRITE=true` so
   paraphrased questions still hit the right chunks.
3. **Chunking.** Tune `CHUNK_TARGET_TOKENS` / `CHUNK_OVERLAP_TOKENS` — smaller chunks
   are more precise, larger ones keep more context. Splitting is header-aware, so
   well-structured docs (real headings) retrieve better. Changing chunking → `reindex`.
4. **The confidence gate.** Lower `CONFIDENCE_THRESHOLD` to answer more (risk: more
   borderline answers); raise it to escalate more (safer).
5. **Better models.** A stronger **embedding** model improves retrieval (re-ingest after
   changing it); a stronger **instruct** LLM grounds and cites better. Keep
   `VERIFY_ANSWERS=true` for the extra grounding check.
6. **Curate known gaps.** For questions your docs don't cover, add approved answers via
   the Inform loop so they're retrievable next time.

**Measure, don't guess:** change one lever at a time and re-run the golden eval
(`python eval/run_eval.py`) to confirm accuracy actually improved.

## Bots & sockets

**Which surfaces are there?** A standalone **web UI** (`frontend/`, no tokens), plus
**Webex**, **Slack**, and **Microsoft Teams** bots — all hitting the same `/ask` API.

**Socket Mode / websocket vs webhook — what's the difference?**
- **Webex & Slack** dial **outbound** (websocket / Socket Mode) → **no public URL**,
  no inbound firewall change.
- **Teams** is **inbound** — Azure Bot Service POSTs to a webhook, so it needs a
  **public HTTPS URL** (a dev tunnel or real domain + cert) and an Azure bot.

**Slack: why two tokens?** They're different and not interchangeable: the **Bot User
OAuth Token** (`xoxb-…` → `SLACK_BOT_TOKEN`, from *OAuth & Permissions* after install)
authorizes the bot; the **App-Level Token** (`xapp-…` → `SLACK_APP_TOKEN`, from *Basic
Information → App-Level Tokens*) authorizes the Socket Mode connection
(`connections:write`). KAI exits with a clear message if they're missing or swapped.

**Slack: which scopes?** `app_mentions:read`, `chat:write`, `im:history` (for DMs).
That's it — `commands`/`im:read` are **not** needed.

**Webex: why don't I see feedback buttons / a scopes screen?** Webex bots have **no
scopes to choose** (fixed capability set). The 👍/👎 card is **opt-in** — set
`WEBEX_FEEDBACK_CARD=true` (Slack's buttons are on by default).

**Can I run more than one platform at once?** Yes — one platform per process; start
several `python run/setup.py bot` processes (each with its `CHAT_PLATFORM`) against the
same API.

**Does the bot reply in-thread?** Yes — answers and feedback acks stay in the original
thread, so several people can use the bot in one space without crossing wires.

**Who can use the bot?** Today only **Webex** has a per-user allowlist
(`WEBEX_APPROVED_USERS` / `WEBEX_APPROVED_DOMAINS`). Slack/Teams answer anyone who can
reach the bot — gate them at the platform level. `KAI_API_KEY` guards the **HTTP API**,
not who may DM the bot.

## Usage patterns (a few ideas, depending on how you use it)

- **Org FAQ bot.** Point KAI at your wiki/Confluence so it fields the repeated
  questions in Slack/Webex/Teams — cited, and escalating when unsure.
- **Personal knowledge base.** Run it locally over your own notes/PDFs/docs.
- **Studying / interview prep.** Load papers or a syllabus and quiz yourself; every
  answer is grounded and cited so you can verify it.
- **"Just the relevant bit" of a big document.** Attach a long PDF/spec in the web UI
  (📎) and ask one pointed question — read once, **not** saved to the corpus.
- **Curated team knowledge.** Use the Inform loop to capture approved answers to things
  that aren't written down anywhere yet.

## Common gotchas

- **The web UI needs no token.** "0 tokens" for the web UI is correct — it's a browser
  app calling the HTTP API; only the **bots** need platform tokens.
- **An uploaded file (📎 / `/ask-document`) is read once and never added to the KB.**
  To add documents permanently, ingest them.
- **CORS is deny-by-default.** The web UI's origin must be in `CORS_ORIGINS` or the
  browser blocks it.
- **Changing the embedding model requires a re-ingest** (`EMBED_DIMENSIONS` must match).
- **`reset-db` / `fresh` are destructive** — they wipe the database. Day-to-day you want
  `ingest`.
- **KAI is intentionally framework-free** (no LangChain/LangGraph) — small and readable;
  the same design extends to those if you want agentic/advanced features.

# Changelog

## 1.0.0

First public release. The "never confidently wrong" guarantee is intact throughout:
every confident answer is grounded in retrieved sources and cited, and anything
unsupported is escalated instead of guessed — validated by the golden eval (`eval/`)
and the automated test suite (**303 tests pass; `ruff` clean**).

This release also adds: a single **"Escalate to a human"** control + a clear marker on
escalated chat replies; feedback buttons that can't be double-submitted (Slack/Teams);
a richer **Swagger/OpenAPI** reference (grouped, summarized endpoints); original bundled
**sample docs**; and a [**FAQ**](doc/faq.md) covering setup, config, LLM, accuracy
tuning, bots/sockets, and usage patterns.

### Pre-release hardening (deep security/accuracy/perf audit)
- **Numeric-fabrication guard** — a deterministic check escalates any answer stating
  a significant number (thousands-separated or ≥5 digits) absent from the sources;
  closes a found case where the model *computed* a host count not in the corpus.
  The LLM verifier prompt now also fails on computed/un-sourced numbers.
- **Secure by default** — CORS no longer defaults to `*` (explicit `CORS_ORIGINS`
  only; wildcard warns); a loud warning when `KAI_API_KEY` is unset; `/metrics` and
  interactive docs/OpenAPI are gated when a key is set.
- **DoS guard** — `/ask` & `/search` questions are capped (`max_length=2000`),
  rejected at validation before any embed/LLM work.
- **Prune never deletes curated answers** (`kai-curated:*` excluded from reconciliation).
- **Cross-instance safety** — Confluence Doc ids are namespaced by host, so the same
  page id on two sites can't collide/overwrite (one-time re-ingest to adopt).
- **Ad-hoc document Q&A** batches embeddings and caps oversized files; incremental
  `doc_hashes()` no longer silently triggers a full re-embed on a transient DB error.

### Retrieval & accuracy
- **Query normalization** — strip conversational/imperative filler ("show me details
  of X" → "X") for retrieval/ranking; fixed natural-phrasing queries that escalated
  even though the page was retrieved #1. Original wording kept for the answer + ticket.
- **Lexical OR-fallback** — when the AND tsquery matches nothing, fall back to an
  OR-of-terms so one absent word can't zero the lexical arm (eval-gated; no change
  when AND has hits).
- **Reranker-aware confidence** (`rerank_score_is_probability`) and a recalibrated
  grounding floor; bge-reranker trialed and rejected (no gain, 5–10× slower).
- **`halfvec` vector type** switch (fp16, measured lossless) for scale.

### Answer-path correctness (audit fixes)
- Code-aware `_tidy_answer` (no longer corrupts `()`/`[]`/indentation in code).
- Context-aware citation renumbering (array indices like `argv[1]` untouched).
- Jira escalation: try/except degrade (no HTTP 500 on tracker outage) + summary
  newline sanitization.
- Prune mass-delete guard; blank-question → 422 (was 500); threshold drift aligned to 0.45.

### Multi-source ingestion
- **Files/PDF** source (`FileKBSource`) + content-type-aware chunking (markdown vs
  plain vs HTML); robust decoding (UTF-16/CP1252, binary/oversized/scanned skipped).
- **Incremental re-ingest** (per-doc content hash — unchanged docs skip embedding).
- **Multiple sources** — many Confluence spaces (comma-separated `CONFLUENCE_SPACE_KEY`),
  many Confluence **instances** (numbered `CONFLUENCE_<n>_*` — Cloud *and* self-hosted
  Server/DC), and many directories (`SOURCE_DIRS`), combined via `CompositeKBSource`.

### Interfaces
- **Web frontend (`frontend/`)** — a standalone, dependency-free chat UI, kept
  **separate** from the backend (its own app/origin, not bundled in the package). It
  calls the API over CORS (`CORS_ORIGINS`, configurable API base in the UI). The API
  is now API-only: `GET /` returns a JSON banner, not HTML.

### Chat platforms (pluggable)
- **`ChatAdapter` abstraction** (`kai/chat/`) — one thin bot per platform over the
  `/ask` API; `CHAT_PLATFORM` selects webex | slack | teams.
- **Webex**: edit-in-place answers, proactive DM, feedback Adaptive Card, threaded
  replies, message splitting, supervised reconnect, **inbound-file Q&A** (drop a PDF →
  ad-hoc RAG via `/ask-document`), configurable copy (`BOT_ACK_MESSAGE`, `BOT_ANSWER_PREFIX`).
- **Slack**: complete adapter (Socket Mode, Block Kit) + paste-to-create manifest.
- **Teams**: inbound Bot Framework webhook adapter (`kai/chat/teams.py`) — same
  `ChatService` + Adaptive Card as Webex/Slack; serves `POST /api/messages`. Needs an
  Azure Bot + a public HTTPS URL (`pip install '.[teams]'`); parsing is unit-tested,
  the live Connector round-trip is verified in-tenant.

### Observability & feedback
- **M3**: per-ask structured event + `kai_questions` + `/metrics` + `/admin/gaps`.
- **M4**: `/feedback` (👍/👎) + `/escalate` + `kai_feedback`.
- **M2**: answer cache (ingest-busted). **M11**: Jira-egress flag. **M12**: httpx keep-alive.

### Inform loop (B1) — learning, safely
- Gaps → human-curated answer → **approval-gated** synthesis into curated KB
  (`space=kai-curated`); the next identical question then answers.
- **Wrong-answer defense:** approval gate · optional 4-eyes (approver≠author) · audit
  trail · **👎-driven auto-quarantine** (downvoted curated answers self-un-index) ·
  one-call `/revoke` · curated answers labeled as community-curated in the reply.

### Ops
- **Deploy**: `Dockerfile` + `docker-compose.yml` (API + pgvector Postgres, optional
  Ollama/bot) + systemd unit (`deploy/kai.service`) + `doc/deploy.md` (provider
  matrix + prod checklist).
- **Import-safe app** — `import kai.app` no longer requires a configured `.env`, so
  the suite/CI run with zero config; a misconfigured server returns a clear 503.
- **Server supervisor** (`run/setup.py supervise`, auto-restart).
- **CI** (`.github/workflows/ci.yml`, full suite, no Ollama needed).
- Dependency extras (`[bot]`, `[slack]`, `[teams]`, `[rerank]`, `[dev]`); `.env.example`
  aligned to the validated config.

### Docs
`doc/integrations-setup.md` (Webex/Slack/Teams setup + tokens) · `doc/chat-platforms.md`
(architecture) · `doc/sources.md` · this changelog.

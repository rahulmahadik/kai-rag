# Changelog

## 1.1.0

Maintenance release: dependency and toolchain refresh, a rebuilt CI pipeline, a real
integration and live-model test suite, and a set of correctness and hardening fixes
found by that new coverage. No new features; no behaviour change to the answer
contract beyond the grounding fixes listed below.

### Fixed
- **`VECTOR_TYPE` change reported an opaque Postgres error.** `ensure_schema` ran its
  index DDL before the column-type check, so switching `vector`/`halfvec` on an
  existing table raised `DatatypeMismatch` from `CREATE INDEX` instead of the
  actionable "re-ingest into a fresh table" message. The check now runs first.
  (Found by the new pgvector integration suite.)
- **A non-ASCII `Authorization` header returned 500 instead of 401.**
  `hmac.compare_digest` raises `TypeError` on a non-ASCII `str`; the bearer check now
  compares bytes, so a malformed key is rejected the same way a wrong one is.
- **A NUL byte in a document body failed that document's whole ingest.** Postgres
  `text` columns reject NUL, which a PDF extraction or an `&#0;` entity in a page
  could produce. Control characters are now stripped once, in the chunker, so every
  ingest path (Confluence, files, uploads, curated answers) is covered.
- **A version string could be flagged as a fabricated number.** The numeric guard
  masked dotted runs out of the source, so an answer citing `1.15` against a source
  saying `1.15.0` looked ungrounded. The source is now scanned both masked and raw.
- **The answer verifier only saw the first 800 characters of each source,** about a
  third of a chunk, so an answer whose supporting sentence sat in the tail could be
  failed. Raised to cover a whole chunk.
- **The HTTP clients could leak connections.** The lazy `httpx.Client` build in the
  LLM and embedding providers was unguarded, so two threadpool requests could each
  build one and drop the loser. Now locked, with explicit pool limits and a `close()`.
- **Citation links in the web UI did not restrict the URL scheme.** Only navigating
  schemes render as links now, so a `javascript:` URL reaching the corpus cannot
  become a live href. `'` is escaped and links carry `rel="noopener noreferrer"`.

### Changed
- **Dependency floors raised** to the versions the suite is green against: FastAPI
  0.138, uvicorn 0.49, Pydantic 2.13, psycopg 3.3, pgvector 0.4, NumPy 2.4, pypdf 6.
  Optional extras follow (slack_bolt 1.28, webex_bot 1.3, sentence-transformers 5).
- **Docker image on Python 3.13** and running as an unprivileged user; Compose now
  uses `pgvector/pgvector:pg18`.
- **Ruff rule set widened** to import order, bugbear, pyupgrade, comprehension,
  pathlib and Ruff-specific checks. Every `zip()` over sequences that must align now
  passes `strict=True`.
- **Comments and prose rewritten** throughout: em-dashes and typographic quotes
  replaced with ordinary punctuation, and multi-paragraph block comments cut to the
  point they were making.

### Fixed (second pass, found by the new coverage and an end-to-end audit)
- **The Webex reconnect backoff never grew.** `backoff` was reset to its base value
  inside the loop body, before `bot.run()`, so `time.sleep(backoff)` always saw 5s:
  a crash-looping bot reconnected flat out instead of backing off. It now resets only
  after a session that stayed up (60s), and the regression test pins the sequence at
  5, 10, 20, 40, 80s up to the 300s cap.
- **`docker compose up` produced a 500 on the first question.** The image builds
  without the `rerank` extra while `.env.example` ships `RERANKER=cross_encoder`, and
  `kai/providers/reranker.py` imported `sentence_transformers` unguarded. Compose now
  defaults `RERANKER=noop`, the import failure names the extra to install or the
  setting to change, and the README states the `cp .env.example .env` prerequisite.
- **The CI non-root check passed vacuously.** `test "$(docker run ...)" != "0"` is
  true when the command fails and the substitution is empty, so a broken image would
  have been reported as non-root. It now asserts the exact uid.
- **`GET /health` on an unconfigured app was documented as returning 503.** It
  returns 200 with `{"status": "unconfigured"}`, which the container healthcheck and
  the CI boot check both depend on.
- The shipped Slack manifest requested `commands` and `im:read`, which the setup docs
  correctly say are not needed. Removed from the manifest.

### CI and tests
- **CI rebuilt** as four jobs: ruff lint, the unit suite on Python 3.12 and 3.13, an
  integration job against a real `pgvector/pgvector:pg18` service container, and a
  Docker job that builds the image, boots it, and checks `/health` and that it is not
  running as root. Adds pip caching, run concurrency, least-privilege permissions, and
  a Dependabot config.
- **Integration suite added** (`tests/integration/`), skipped unless
  `KAI_TEST_DATABASE_URL` is set: the pgvector store against real SQL, the
  ingest-to-answer pipeline over real rows, and telemetry plus the Inform queue.
- **Live-model smoke tests added**, skipped unless `KAI_TEST_LLM_BASE_URL` is set.
  These exercise the actual promise against a real model: an in-scope question is
  answered with citations, an out-of-scope one escalates, and a plausible but
  uncovered detail is not invented. Verified against `qwen2.5:14b-instruct` plus
  `nomic-embed-text` on Ollama.
- **Web-UI suite added** (`tests/integration/test_web_ui.py`): loads
  `frontend/index.html` in real Chromium via Playwright and asserts on the rendered
  DOM, including that answer markup is escaped and that a citation URL with a
  non-navigating scheme never becomes a live link. Installed and run in CI.
- **Coverage raised from 65% to 94%** (303 tests to 697), with a 92% floor enforced
  in CI. New coverage for the HTTP providers, the admin and Inform API surface, the
  LLM helper wrappers, the reranker, provider wiring, the bot launcher, and all three
  chat adapters driven through injected fake SDKs.
- **CI hardening**: `timeout-minutes` on every job, a pinned ruff in the lint job (an
  unpinned floor lets a new release fail every open PR with no code change), and
  `versioning-strategy: increase` so Dependabot actually raises the `>=` floors.

## 1.0.0

First public release. The "never confidently wrong" guarantee is intact throughout:
every confident answer is grounded in retrieved sources and cited, and anything
unsupported is escalated instead of guessed, validated by the golden eval (`eval/`)
and the automated test suite (**303 tests pass; `ruff` clean**).

This release also adds: a single **"Escalate to a human"** control + a clear marker on
escalated chat replies; feedback buttons that can't be double-submitted (Slack/Teams);
a richer **Swagger/OpenAPI** reference (grouped, summarized endpoints); original bundled
**sample docs**; and a [**FAQ**](doc/faq.md) covering setup, config, LLM, accuracy
tuning, bots/sockets, and usage patterns.

### Pre-release hardening (deep security/accuracy/perf audit)
- **Numeric-fabrication guard**: a deterministic check escalates any answer stating
  a significant number (thousands-separated or ≥5 digits) absent from the sources;
  closes a found case where the model *computed* a host count not in the corpus.
  The LLM verifier prompt now also fails on computed/un-sourced numbers.
- **Secure by default**, CORS no longer defaults to `*` (explicit `CORS_ORIGINS`
  only; wildcard warns); a loud warning when `KAI_API_KEY` is unset; `/metrics` and
  interactive docs/OpenAPI are gated when a key is set.
- **DoS guard**, `/ask` & `/search` questions are capped (`max_length=2000`),
  rejected at validation before any embed/LLM work.
- **Prune never deletes curated answers** (`kai-curated:*` excluded from reconciliation).
- **Cross-instance safety**, Confluence Doc ids are namespaced by host, so the same
  page id on two sites can't collide/overwrite (one-time re-ingest to adopt).
- **Ad-hoc document Q&A** batches embeddings and caps oversized files; incremental
  `doc_hashes()` no longer silently triggers a full re-embed on a transient DB error.

### Retrieval & accuracy
- **Query normalization**, strip conversational/imperative filler ("show me details
  of X" → "X") for retrieval/ranking; fixed natural-phrasing queries that escalated
  even though the page was retrieved #1. Original wording kept for the answer + ticket.
- **Lexical OR-fallback**: when the AND tsquery matches nothing, fall back to an
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
- **Incremental re-ingest** (per-doc content hash, unchanged docs skip embedding).
- **Multiple sources**, many Confluence spaces (comma-separated `CONFLUENCE_SPACE_KEY`),
  many Confluence **instances** (numbered `CONFLUENCE_<n>_*`, Cloud *and* self-hosted
  Server/DC), and many directories (`SOURCE_DIRS`), combined via `CompositeKBSource`.

### Interfaces
- **Web frontend (`frontend/`)**: a standalone, dependency-free chat UI, kept
  **separate** from the backend (its own app/origin, not bundled in the package). It
  calls the API over CORS (`CORS_ORIGINS`, configurable API base in the UI). The API
  is now API-only: `GET /` returns a JSON banner, not HTML.

### Chat platforms (pluggable)
- **`ChatAdapter` abstraction** (`kai/chat/`): one thin bot per platform over the
  `/ask` API; `CHAT_PLATFORM` selects webex | slack | teams.
- **Webex**: edit-in-place answers, proactive DM, feedback Adaptive Card, threaded
  replies, message splitting, supervised reconnect, **inbound-file Q&A** (drop a PDF →
  ad-hoc RAG via `/ask-document`), configurable copy (`BOT_ACK_MESSAGE`, `BOT_ANSWER_PREFIX`).
- **Slack**: complete adapter (Socket Mode, Block Kit) + paste-to-create manifest.
- **Teams**: inbound Bot Framework webhook adapter (`kai/chat/teams.py`), same
  `ChatService` + Adaptive Card as Webex/Slack; serves `POST /api/messages`. Needs an
  Azure Bot + a public HTTPS URL (`pip install '.[teams]'`); parsing is unit-tested,
  the live Connector round-trip is verified in-tenant.

### Observability & feedback
- **M3**: per-ask structured event + `kai_questions` + `/metrics` + `/admin/gaps`.
- **M4**: `/feedback` (👍/👎) + `/escalate` + `kai_feedback`.
- **M2**: answer cache (ingest-busted). **M11**: Jira-egress flag. **M12**: httpx keep-alive.

### Inform loop (B1), learning, safely
- Gaps → human-curated answer → **approval-gated** synthesis into curated KB
  (`space=kai-curated`); the next identical question then answers.
- **Wrong-answer defense:** approval gate · optional 4-eyes (approver≠author) · audit
  trail · **👎-driven auto-quarantine** (downvoted curated answers self-un-index) ·
  one-call `/revoke` · curated answers labeled as community-curated in the reply.

### Ops
- **Deploy**: `Dockerfile` + `docker-compose.yml` (API + pgvector Postgres, optional
  Ollama/bot) + systemd unit (`deploy/kai.service`) + `doc/deploy.md` (provider
  matrix + prod checklist).
- **Import-safe app**, `import kai.app` no longer requires a configured `.env`, so
  the suite/CI run with zero config; a misconfigured server returns a clear 503.
- **Server supervisor** (`run/setup.py supervise`, auto-restart).
- **CI** (`.github/workflows/ci.yml`, full suite, no Ollama needed).
- Dependency extras (`[bot]`, `[slack]`, `[teams]`, `[rerank]`, `[dev]`); `.env.example`
  aligned to the validated config.

### Docs
`doc/integrations-setup.md` (Webex/Slack/Teams setup + tokens) · `doc/chat-platforms.md`
(architecture) · `doc/sources.md` · this changelog.

# Changelog

## 1.2.8

### Added
- **Space-based escalation notify routing.** `NOTIFY_ROUTES` maps space/source
  tags to a chat recipient (`ENG:alice@…;*:oncall@…`). On `/ask` or `/escalate`
  escalation, KAI DMs the matched on-call via `CHAT_PLATFORM` (best-effort;
  response may include `escalation_notified`). Citations now carry `space`.

## 1.2.7

### Added
- **Email (SMTP) escalation.** When Jira and GitHub Issues are unset,
  `ESCALATION_EMAIL_TO` / `FROM` / `ESCALATION_SMTP_*` send the escalation over
  SMTP and return a `mailto:` contact link. Preference: Jira → GitHub → email →
  local log.

## 1.2.6

### Docs
- Public docs catch up to shipped 1.2.x behaviour: Slack/Teams file attach +
  allowlists, URL crawl (robots, Basic auth, PDF), `/notify`, `ASK_SPACES` /
  `ASK_SPACE_ACL`, and the architecture/source tree. Removed stale "on the
  roadmap" wording that contradicted the code.

## 1.2.5

### Added
- **Authenticated URL crawl.** `SOURCE_URL_USER` / `SOURCE_URL_PASSWORD` send
  HTTP Basic credentials on every fetch (seeds, sitemap, followed links, robots).
- **PDF pages in the URL crawl.** Same-host `.pdf` links (and PDF seeds) are
  fetched and text-extracted with pypdf, same path as local file ingest.

## 1.2.4

### Added
- **URL crawl respects robots.txt** (`SOURCE_URL_RESPECT_ROBOTS`, default on).
  Per-origin `/robots.txt` is fetched once per ingest; disallowed paths are skipped.

## 1.2.3

### Added
- **Per-space answer scoping.** `ASK_SPACES` (global allowlist), `ASK_SPACE_ACL`
  (per email / user id / `@domain`, fail-closed), and request fields
  `spaces` / `asker` on `/ask` and `/search`. Chat bots pass the sender as
  `asker`. Filtered retrieval includes `kai-curated` by default
  (`ASK_INCLUDE_CURATED`). Answer cache keys include scope so answers cannot
  leak across ACL boundaries.

## 1.2.2

### Added
- **Teams proactive DM.** The Teams adapter stores a conversation reference when
  a user messages the bot; `POST /notify` and Inform approval can then DM that
  user via the Bot Connector (recipient = AAD object id / user id). Cold-open
  without a prior message is still impossible on Bot Framework.

## 1.2.1

### Added
- **Same-host URL crawl.** With `SOURCE_URL_FOLLOW_LINKS` (default on), the URL
  source follows `<a href>` links on the same host until `SOURCE_URL_MAX_PAGES`.

### Fixed
- **`POST /notify` returned 502 for a bad Slack recipient.** Invalid user ids
  are now **400** (client error), not an upstream failure.

## 1.2.0

Product release: more ways to escalate, ingest, notify, and curate, without
changing the never-fabricate answer contract.

### Added
- **GitHub Issues tracker.** Set `GITHUB_TOKEN` + `GITHUB_REPO` (`owner/name`) to
  file escalations as GitHub Issues (Enterprise via `GITHUB_API_URL`). Jira still
  wins when fully configured; otherwise GitHub; otherwise local log.
- **URL / sitemap knowledge source.** `SOURCE_TYPE=urls` (or `…+urls`) with
  `SOURCE_URLS` and/or `SOURCE_SITEMAP`, capped by `SOURCE_URL_MAX_PAGES`.
- **Slack and Teams allowlists.** `SLACK_APPROVED_USERS` and `TEAMS_APPROVED_USERS`
  mirror Webex's allowlist pattern (blank = anyone who can reach the bot).
- **File attachments on Slack and Teams.** Attached files run through
  `/ask-document` (same ad-hoc path as Webex and the web UI). Slack needs
  `files:read`; Teams uses `contentUrl` when the activity carries it.
- **Cross-platform `/notify`.** Proactive DM on Webex or Slack (selected by
  `CHAT_PLATFORM`). Body accepts `recipient` (preferred) or legacy `email`.
  Inform approval DMs the asker on the active platform. Teams cold-DM stays
  unsupported (needs a stored conversation reference).
- **Inform review UI.** `frontend/admin.html` lists pending candidates and
  supports approve / reject / revoke / submit (same `/admin/inform*` API).
- **Scheduled ingest helper.** `python run/setup.py schedule-ingest` prints cron
  and systemd timer snippets that call the existing ingest command.

### Changed
- Version bumped to **1.2.0**. Slack manifest scopes include `files:read`,
  `im:write`, and `users:read.email` for attachment download and notify-by-email.

## 1.1.1

Maintenance release: dependency floors raised against a green suite, public docs
tightened, and a few leftover inconsistencies cleaned up. No change to the
answer contract.

### Changed
- **Dependency floors raised** to the versions the suite is green against:
  FastAPI 0.163, uvicorn 0.54, Pydantic 2.14, pydantic-settings 2.16, psycopg
  3.3.9, pgvector 0.5.1, NumPy 2.5.4, pypdf 7.1, plus optional extras
  (slack_bolt 1.30, webex_bot 1.3.1, sentence-transformers 6.3, playwright
  1.65, pyjwt 2.15). Ruff CI pin moved to 0.17.5 with the `dev` extra.
- **Public README rewritten** as a short landing page: one install path, links
  into `doc/` for depth. Setup docs, FAQ, and CONTRIBUTING now agree that
  `setup.py install` creates `.env` from `.env.example` when missing.
- **Python classifiers** list 3.12, 3.13, and 3.14 (matching the CI matrix and
  the `python:3.14-slim` image). Repository URL added to `pyproject.toml`.
- **Dependabot config removed.** Dependency floors are raised deliberately, as
  part of a change that carries its own testing, rather than arriving as
  automated pull requests.

### Fixed
- **Slack scopes comment in `.env.example`** still listed `commands`. Aligned
  with the manifest and FAQ: `app_mentions:read`, `chat:write`, `im:history`.

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
- **Dependency floors raised** to the versions the suite was green against at
  release: FastAPI 0.138, uvicorn 0.49, Pydantic 2.13, psycopg 3.3, pgvector
  0.4, NumPy 2.4, pypdf 6. Optional extras follow (slack_bolt 1.28, webex_bot
  1.3, sentence-transformers 5). Later floors are in 1.1.1.
- **Docker image on Python 3.14** and running as an unprivileged user; Compose
  uses `pgvector/pgvector:pg18`. (The image moved from 3.13 to 3.14 during the
  1.1.0 Dependabot cycle before Dependabot was removed.)
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
  setting to change, and the README states the `.env` prerequisite.
- **The CI non-root check passed vacuously.** `test "$(docker run ...)" != "0"` is
  true when the command fails and the substitution is empty, so a broken image would
  have been reported as non-root. It now asserts the exact uid.
- **`GET /health` on an unconfigured app was documented as returning 503.** It
  returns 200 with `{"status": "unconfigured"}`, which the container healthcheck and
  the CI boot check both depend on.
- The shipped Slack manifest requested `commands` and `im:read`, which the setup docs
  correctly say are not needed. Removed from the manifest.

### CI and tests
- **CI rebuilt** as four jobs: ruff lint, the unit suite on Python 3.12 / 3.13 /
  3.14, an integration job against a real `pgvector/pgvector:pg18` service
  container, and a Docker job that builds the image, boots it, and checks
  `/health` and that it is not running as root. Adds pip caching, run
  concurrency, and least-privilege permissions.
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
- **CI hardening**: `timeout-minutes` on every job, and a pinned ruff in the lint
  job (an unpinned floor lets a new release fail every open PR with no code
  change).

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
  plain text vs HTML).
- **Multiple Confluence instances** and multiple file directories ingested together.
- Incremental ingest with content hashing; prune of removed docs.

# Roadmap

KAI is a complete, self-hostable knowledge assistant. It ingests your docs, answers
grounded-and-cited questions over them, and escalates instead of guessing, end to end
today. This page records what ships now and what's planned next. Contributions welcome.

## Done

- **Retrieval**, hybrid dense + lexical (full-text) search fused with RRF,
  cross-encoder rerank, a calibrated confidence gate, and grounding/verification
  guards that escalate instead of fabricating (validated by the golden eval in
  `eval/` + the automated test suite).
- **Sources**, Confluence with **multiple spaces and multiple instances**
  (Cloud *and* self-hosted Server/Data Center), local files/PDF/Markdown/HTML
  with **multiple directories**, and **URL / sitemap** pages (`SOURCE_TYPE=urls`),
  all ingested together. `setup.py schedule-ingest` prints cron/systemd snippets.
- **Interfaces**: a standalone web chat UI (`frontend/`) with file upload + an
  Inform review page (`admin.html`), the HTTP API, and Webex + Slack + Teams
  bots (Teams: verify in your Azure tenant). Allowlists and file attachments on
  all three platforms.
- **Escalation**, opens a **Jira** or **GitHub Issues** ticket on low-confidence
  questions, and can **proactively DM** on Webex, Slack, or Teams (`/notify`;
  Teams uses a stored conversation reference after the user messages the bot).
- **Learning loop**, approval-gated curated answers (API + `frontend/admin.html`),
  with an audit trail and 👎-driven auto-removal; observability via `/metrics`
  and `/admin/gaps`.
- **Ad-hoc document Q&A**, drop a file (`POST /ask-document`, 📎 in the web UI, or
  attach in Webex/Slack/Teams) and ask about *just that file*; read once,
  answered with the same never-fabricate guards, never written to the corpus.
- **Retrieve-only search**, `POST /search` returns the ranked chunks + scores + the
  escalate decision (no answer generation) for evaluation and debugging.
- **Deploy**, Docker Compose + systemd; any OpenAI-compatible LLM/embeddings,
  self-hosted or hosted.

## Planned

### More knowledge sources
- Authenticated crawl and PDF page extraction; connectors for SharePoint,
  Google Drive, and Git repos/wikis. Same-host link following and robots.txt
  already ship (`SOURCE_URL_FOLLOW_LINKS`, `SOURCE_URL_RESPECT_ROBOTS`).

### Escalation & notifications, *raise a ticket / page a person*
- Trackers beyond Jira/GitHub: ServiceNow, Linear, GitLab, or plain email.
- Topic/space-based notify routing with acknowledgement; richer Teams
  conversation creation (open a fresh 1:1 when only a channel ref exists).
- Escalation templates and auto-assignment by subject area.

### Answering & UX
- Richer ACL (group/role sync from IdP); per-source tags beyond Confluence space.
- Feedback-driven retrieval tuning.

> **Shipped:** per-space answer scoping via `ASK_SPACES` / `ASK_SPACE_ACL` and
> request `spaces`/`asker` on `/ask` and `/search` (chat bots send the sender as
> `asker`).

> **Not** token-streaming: KAI's grounding/verification guards run *after*
> generation, so streaming raw tokens would show text that a guard might then
> retract. The edit-in-place "Searching... → answer" ack is the deliberate
> alternative. See [chat-platforms.md](chat-platforms.md).

> Most of these are **additive**: the provider Protocols (`KBSource`, `Tracker`,
> `ChatAdapter`) are designed so a new source, tracker, or chat surface drops in
> without touching the retrieval/answering core. See
> [architecture.md](architecture.md).

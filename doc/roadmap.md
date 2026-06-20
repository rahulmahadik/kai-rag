# Roadmap

KAI is a complete, self-hostable knowledge assistant — it ingests your docs, answers
grounded-and-cited questions over them, and escalates instead of guessing, end to end
today. This page records what ships now and what's planned next. Contributions welcome.

## Done

- **Retrieval** — hybrid dense + lexical (full-text) search fused with RRF,
  cross-encoder rerank, a calibrated confidence gate, and grounding/verification
  guards that escalate instead of fabricating (validated by the golden eval in
  `eval/` + the automated test suite).
- **Sources** — Confluence with **multiple spaces and multiple instances**
  (Cloud *and* self-hosted Server/Data Center), plus local files/PDF/Markdown/HTML
  with **multiple directories** — all ingested together.
- **Interfaces** — a standalone web chat UI (`frontend/`) with file upload, the HTTP
  API, and Webex + Slack bots; a Microsoft Teams adapter is included (verify in your
  own Azure tenant).
- **Escalation** — opens a **Jira ticket** on low-confidence questions, and can
  **proactively DM a user** on Webex (`/notify`).
- **Learning loop** — approval-gated curated answers, with an audit trail and
  👎-driven auto-removal; observability via `/metrics` and `/admin/gaps`.
- **Ad-hoc document Q&A** — drop a file (`POST /ask-document`, or 📎 in the web UI)
  and ask about *just that file*; read once, answered with the same never-fabricate
  guards, never written to the corpus.
- **Retrieve-only search** — `POST /search` returns the ranked chunks + scores + the
  escalate decision (no answer generation) for evaluation and debugging.
- **Deploy** — Docker Compose + systemd; any OpenAI-compatible LLM/embeddings,
  self-hosted or hosted.

## Planned

### More knowledge sources
- **Web / URL crawler** — ingest any docs site or `sitemap.xml`. This is the
  general path for **non-Confluence wikis** (MediaWiki, DokuWiki, etc.) and plain
  documentation websites.
- Connectors for SharePoint, Google Drive, and Git repos/wikis (GitHub/GitLab).

### Escalation & notifications — *raise a ticket / page a person*
- **Pluggable trackers beyond Jira** — GitHub/GitLab issues, ServiceNow, Linear,
  or plain email, behind the existing `Tracker` interface.
- **Personal paging** — DM the right person or on-call directly on Slack/Teams
  (today: Webex DM), with topic/space-based routing and acknowledgement.
- Escalation templates and auto-assignment by subject area.

### Answering & UX
- Per-space / per-source access control and answer scoping.
- Per-user access control for Slack/Teams (Webex has it today).
- Feedback-driven retrieval tuning.

> **Not** token-streaming: KAI's grounding/verification guards run *after*
> generation, so streaming raw tokens would show text that a guard might then
> retract. The edit-in-place "Searching… → answer" ack is the deliberate
> alternative. See [chat-platforms.md](chat-platforms.md).

> Most of these are **additive** — the provider Protocols (`KBSource`, `Tracker`,
> `ChatAdapter`) are designed so a new source, tracker, or chat surface drops in
> without touching the retrieval/answering core. See
> [architecture.md](architecture.md).

# Security Policy

## Reporting a vulnerability

Please report security issues **privately** — do **not** open a public issue or PR.

Use GitHub's [private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
(the **Security** tab → *Report a vulnerability*) on this repository. Include:

- a description and the impact,
- steps to reproduce (or a proof of concept),
- affected version / commit.

We aim to acknowledge reports promptly and will coordinate a fix and disclosure.

## Hardening checklist (operators)

KAI is secure-by-default for local dev but you **must** configure these before
exposing it beyond `localhost`:

- **`KAI_API_KEY`** — set it. Unset means the API is unauthenticated and the **whole
  corpus is readable** by anyone who can reach it. When set, `/docs` and the
  `/admin/*` surface are hidden from anonymous callers.
- **`CORS_ORIGINS`** — deny-by-default. Set it to your web UI's **exact** origin;
  never use `*` with an internet-exposed, unauthenticated API.
- **Chat access control** — only **Webex** has a per-user allowlist
  (`WEBEX_APPROVED_USERS` / `WEBEX_APPROVED_DOMAINS`). **Slack and Teams answer anyone**
  who can reach the bot — restrict them at the platform level (private channel /
  scoped Teams app). Teams refuses inbound requests entirely unless `TEAMS_APP_ID`
  is configured.
- **Data egress** — escalation to an external tracker (Jira) sends the question (and,
  only if `ESCALATION_INCLUDE_DRAFT=true`, the unverified model draft). Review before
  enabling in residency-sensitive deployments.
- **Uploads** — `POST /ask-document` reads a file once for that question and never
  stores it; `FILE_MAX_BYTES` caps the size.
- **Rate limiting** — KAI does not rate-limit in-process. `/ask` and `/ask-document`
  trigger embedding + LLM calls (cost/DoS surface), so put KAI behind a reverse proxy
  (nginx/Caddy/Cloudflare) with per-IP limits before exposing it, and prefer a
  **separate, stronger token** for the `/admin/*` and `/ingest` routes.

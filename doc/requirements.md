# KAI, Design & Rationale

> Why KAI is built the way it is: the model-agnostic design, the tech choices and
> their trade-offs, and what's deliberately left as **future scope**. For what to run
> today, see the [README](../README.md) and [architecture.md](architecture.md). The
> **v1 core** described here is **implemented and working**; anything labelled
> **Future scope** is not built yet.

---

## 1. Vision

Employees should be able to ask a question in chat and get a **trustworthy, cited answer** drawn from the organization's own knowledge, starting with Confluence. KAI:

- **Know**, ingests org knowledge into a searchable, citation-ready index.
- **Ask**, answers questions via retrieval-augmented generation (RAG) **with citations** back to the source pages.
- **Inform**: when it is **not confident**, it escalates to a human (opens a Jira issue when configured). Validated human answers can be curated back into the knowledge base through an explicit approval step (the Inform loop), always approval-gated, **never auto-published**.

The guiding constraint: **model-agnostic by design**. The chat LLM and the embedding model are chosen by **config**, not code. We start cheap and local, and upgrade to larger local or cloud models with a config change.

---

## 2. What KAI does (the v1 core)

The whole loop, end to end: **ask → retrieve → cited answer → escalate-if-unsure**, plus learn-back through human approval.

**Built and working:**
- **Knowledge**, Confluence (Cloud *and* self-hosted Server/DC), **multiple spaces and instances**, plus local files (PDF / Markdown / text / HTML) across **multiple directories**, all ingested together.
- **`/ask` HTTP API**, `POST /ask {question}` → `{answer, citations[], confidence, escalated, escalation_url, suggested_sources[]}`; plus `/ask-document` (ad-hoc file Q&A, never stored), `/search`, `/feedback`, `/escalate`, and the `/admin/*` maintainer routes.
- **RAG with citations**, every confident answer cites the source pages for the chunks it used; an answer with no supporting chunks is never emitted as confident.
- **Never-fabricate**: a retrieval confidence gate + deterministic grounding guards + an optional LLM verification pass escalate instead of guessing.
- **Escalation**, opens a Jira issue when configured (otherwise logs locally, with no fake link).
- **Inform (learn-back)**, gap/escalated questions can be answered by a human and, **on approval**, curated back into the KB: never auto-published (optional 4-eyes; 👎 auto-quarantine).
- **Surfaces**: a standalone web chat UI and Webex / Slack / Teams bots, all thin clients over the same `/ask`.
- **Model-agnostic**, LLM, embeddings, vector store and reranker are all chosen by config.

**Deliberately out of scope (future):**
- ❌ No SSO / RBAC / multi-tenant isolation (a single `KAI_API_KEY` gates the API; Webex has a per-user allowlist).
- ❌ No HA / clustering / job queues.
- ❌ No per-user OAuth for Atlassian/Webex.

**Single service account per platform** (one Confluence token, one Jira token, one bot token per chat platform). No per-user auth.

---

## 3. Architecture (simple)

```text
INGEST  (batch / scheduled)
  Confluence (multiple spaces/instances) + local files (PDF / MD / HTML / txt)
      └─▶ chunk + clean ─▶ embed ─▶ Vector Store   (Postgres/pgvector: dense + full-text)

ASK  (per question)
  question ─▶ embed ─▶ hybrid retrieve ─▶ rerank ─▶ confidence + grounding gate
      ├─ cleared ──────────▶ grounded prompt ─▶ LLM ─▶ answer + citations + confidence
      └─ low / unsupported ─▶ escalate ─▶ Tracker  (Jira when configured, else local)

SURFACES
  Web UI · Webex · Slack · Teams   ──   thin clients, all over   POST /ask
```

Two flows, one engine:
- **Ingest** (batch / scheduled): Confluence → clean storage-format HTML → chunk → embed → upsert.
- **Ask** (real-time / per question): question → embed → hybrid retrieve → (optional rerank) → prompt → LLM → cited answer + confidence → escalate if unsure.

**What is local versus what needs the network.** A "local model" does not mean an "offline system." The **AI compute runs locally/self-hosted**: the LLM, the embedding model, and the vector store all run on our own box, so **questions and document text never leave it for the AI part** (no external AI API). But the **integrations are network services**: **Webex always needs internet** (it's a cloud platform), and Confluence/Jira need to reach their host (cloud, or our own server if self-hosted). So KAI keeps the *intelligence* on-prem while still talking to the chat, knowledge, and ticket systems over the network. It is not an air-gapped/offline system.

Each stage sits **behind a small interface** (vector store, embedder, reranker, prompt-builder, LLM client, KB source, chat bot, tracker) so any one piece can be swapped by config without touching the others.

---

## 4. Model-agnostic by design

**Core principle:** the chat LLM and embedding model are config values, never hardcoded. A single **OpenAI-compatible client** (`base_url` + `api_key` + `model`) drives every chat backend that speaks the OpenAI Chat Completions wire format: which is all of the realistic targets (local servers and cloud APIs alike).

**Swapping the CHAT model is free**, edit config (`base_url`, `api_key`, `model`), restart, done. No application code changes.

**Swapping the EMBEDDING model is NOT free**, embeddings from one model are not comparable to another's vector space, and a dimension change is physically incompatible with the existing index column. **Changing the embedding model forces a full re-index** of the corpus: edit config (model + dimension) → recreate the vector table → re-embed and re-ingest every document. Still **no code change**, but real time/cost proportional to corpus size. Keep **EmbeddingConfig separate from chat LLMConfig** so chat upgrades stay free and you only pay the re-index when you deliberately change the embedder.

### Current config vs higher config

**Chat LLM**
| | v1 (built) | Higher config (future) |
|---|---|---|
| Model | Local ~14B (e.g. a 14B coder/instruct class) | Larger local model on a GPU box (served via a high-throughput inference server), or a a hosted frontier-model API (config-swappable) |
| Endpoint | Local OpenAI-compatible server | Self-hosted inference server `.../v1`, or a cloud `/v1` endpoint |
| Code change to swap | **None**, single client reads `base_url`/`api_key`/`model` from config | **None** for plain chat; add a thin native adapter **only** if you want provider-native features (e.g. prompt caching, extended thinking, native citations) |
| Trade-off | Cheapest, fully local/on-prem, tight context budget | Better reasoning + larger context; per-token cost and/or GPU spend; data may leave the box (check residency) |

**Embedding model**
| | v1 (built) | Higher config (future) |
|---|---|---|
| Model | `nomic-embed-text v1.5` (768-dim, 8192-token ctx, ~0.5 GB, Apache-2.0) | Stay-local: `Qwen3-Embedding-4B` or `BGE-M3` (multilingual + long-context). Go-API: `Voyage voyage-3-large` (best retrieval, esp. code), `OpenAI text-embedding-3-large` (safe default), or `Cohere embed-v4` (multilingual-first) |
| Quality vs cost | Lower benchmark scores but plenty for v1; near-zero footprint leaves RAM for the LLM | Higher retrieval quality / multilingual; API options add per-token cost and send corpus text off-box |
| Code change to swap | **None** (once model-name is a config field) | **None** |
| **Re-index cost** | n/a | **Required**, full re-embed + re-index of the whole corpus |

**Vector DB**
| | v1 (built) | Higher config (future) |
|---|---|---|
| Engine | **Postgres + pgvector** (docs + embeddings in one DB) | `pgvectorscale` to ~50M vectors; Qdrant cluster (sharding + replication); Milvus only at genuine hyperscale (500M+) |
| Hybrid search | Dense + **Postgres full-text** (`tsvector`) fused with RRF in one query | + the cross-encoder rerank stage (on in the shipped `.env.example`) on the top-k shortlist |
| Filtering | space / label / url / doc_id | Qdrant in-graph filtering stays fast on selective filters at 100M+; pgvector post-filtering degrades on large selective sets |
| Migration | One service (or zero extra with pgvector) | **Same engine → config flip, no migration.** Different engine → full re-embed/re-ingest (vectors aren't portable) |

**Reranker**
| | v1 (built) | Higher config (future) |
|---|---|---|
| Engine | **Cross-encoder** (`ms-marco-MiniLM`, via `sentence-transformers`) on the top-k shortlist, enabled by the shipped `.env.example` (the in-code default is `noop`) | A stronger local cross-encoder (`bge-reranker-v2-m3`) or a hosted reranker (Cohere/Voyage) |
| Selection | Config (`reranker = cross_encoder`; `noop` to disable) | Config (`reranker_model = ...`): no call-site change |
| Trade-off | Lowest latency, simplest | Higher precision; added latency and (for hosted) cost + off-box text |

---

## 5. Tech choices (v1 pick + upgrade path + swap note)

### 5.1 Vector database
- **v1 pick: Postgres + pgvector.** The lowest-ops option, since documents and embeddings live in one database (one transaction, SQL filtering), with **hybrid** retrieval = dense (`<=>` cosine over an HNSW index) + **Postgres full-text** (`tsvector`/`websearch_to_tsquery`, GIN index) fused with RRF in a single query. **Alternative at scale:** Qdrant, a single Rust binary with native hybrid + in-HNSW metadata filtering that stays fast at 100M+ vectors; pick it (or `pgvectorscale`) when pgvector's post-filtering becomes the bottleneck.
- **Upgrade path:** **pgvector scales a long way** before you need to move, `pgvectorscale` stays viable to ~50M vectors; beyond that, Qdrant single-node → cluster is a **config flip, not a migration**; Milvus only at 500M+ with a platform team; Weaviate is a strong alternative if you want built-in vectorizer modules + per-tenant isolation.
- **Avoid for this use case:** Chroma (no native full-text hybrid) and LanceDB (embedded library, not a shared server), choosing either forces a **re-architecture**, not a config swap, when you add hybrid or go multi-user.
- **Swap note:** put a thin **`VectorStorePort`** (`upsert`, `search(query_vector, query_text, top_k, filters) -> ScoredChunk[]`, `delete`, `ensure_collection`) in front of the DB; app code never imports a vendor SDK. Select the adapter by config (`vector_store = qdrant | pgvector | weaviate`). **Honest caveats config can't hide:** (a) **re-index cost is real**, vectors aren't portable across engines; (b) **hybrid fusion math + BM25 tokenization differ per engine**, so tuned weights and result ordering shift after a swap (keep a golden-question eval set to re-validate); (c) **filter expressiveness differs**, normalize filters in your own `Filter` type.

### 5.2 Embeddings
- **v1 pick: `nomic-embed-text v1.5`**, 768-dim, **8192-token context** at a tiny ~0.5 GB footprint, Apache-2.0, fast to start, leaves almost all of 24 GB for the LLM. **Quality-first English alternative:** `bge-large-en-v1.5` (1024-dim, ~1.3 GB, top of the classic open small models). **Avoid `e5-large-v2`** for new work, its 512-token cap truncates real enterprise chunks.
- **Upgrade path:** stay-local → `Qwen3-Embedding-4B` or `BGE-M3` (multilingual, long context); go-API → `Voyage voyage-3-large` (best retrieval, strong on code), `OpenAI text-embedding-3-large` (safe default), or `Cohere embed-v4` (multilingual-first).
- **Swap note:** **changing the embedding model invalidates every stored vector**, plan a full re-embed/re-index. Operationally: bump the dimension config (768 → 1024 → 1536/3072), drop+recreate the vector table, re-ingest. **Never mix model/dimension in one table**, retrieval goes silently wrong. Keep the **model name itself a config field** (e.g. `embedding_model`, `embedding_provider`, `vector_dimensions`) so the swap is a pure config edit, not a code edit.

### 5.3 RAG framework
- **v1 pick: a lightweight DIRECT pipeline**, `retrieve → rerank → prompt → answer` as ~150–250 lines you fully own, using a library **only** for the hard parts (vector store + embeddings client) and **one** OpenAI-compatible client for the LLM. This gives the fewest dependencies, the clearest data flow, **zero hidden prompt mutation**, and near-zero per-query overhead: which matters most with a 14B model where every context token counts. **Do not** adopt a full orchestration framework for single-hop Q&A.
- **Upgrade path:** keep each stage behind a small interface so migration is incremental, not a rewrite. Reach for **LlamaIndex** if RAG deepens (multi-hop, routers, sub-questions, many connectors), **Haystack** if the priority is typed/observable/testable production pipelines, and **LangChain/LangGraph** only when the assistant must become a tool-calling **agent**.
- **Swap note:** model-agnostic LLM = **one OpenAI-compatible client + config, zero per-provider code**. Define `LLMConfig {provider, base_url, api_key, model, timeout}`, instantiate one client, call `chat.completions.create(...)`. **Honest caveats:** (a) some providers' OpenAI-compat layers are convenience shims, plain chat works with no code change, but you lose provider-native features (add a thin native adapter behind the same interface if you later want them); (b) only OpenAI-shaped endpoints (Bearer auth + `/chat/completions` + `/embeddings`) work by config: a non-standard auth header or URL scheme would need a thin adapter; (c) swapping the **chat** model is free, swapping the **embedding** model forces a re-index, keep the two configs separate.

### 5.4 LLM (chat)
- **v1 pick:** a **local ~14B instruct/coder-class model** behind an OpenAI-compatible local server. Cheapest, on-prem, no data leaves the box. Tight context budget, hand-tune the prompt to fit. (Use a **general instruct** model for Q&A, not a code-specialized one.)
- **Upgrade path:** larger local model on a GPU box via a high-throughput inference server, or a a hosted frontier-model API (config-swappable), **swap by config only** (`base_url` + `api_key` + `model`).
- **Swap note:** for plain chat there is never an application-code change. Add a thin **native** adapter (behind the same LLM interface) **only** if you want provider-native features like prompt caching, extended thinking, or native citations: the RAG pipeline stays untouched.

### 5.5 Integrations (thin, single-account)
All three collapse to a handful of authenticated HTTPS calls with one secret each. **Each provider sits behind a tiny Protocol** so a later vendor swap (Confluence→SharePoint, Jira→ServiceNow, Webex→Slack/Teams) is a new adapter, not a rewrite.

| Integration | v1 approach | Genuinely hard part | Later (scale) |
|---|---|---|---|
| **Webex bot** (optional/next) | `webex-bot` library over **websocket**: one bot token, **no public URL / ngrok / inbound firewall** | Adaptive Card buttons fire a separate `attachmentActions:created` event you must handle; bot only sees group messages when **@mentioned**, and you must strip the mention markup | Registered HTTPS webhooks + signature verification for HA/multi-instance |
| **Confluence read** | Plain HTTPS GET, `HTTPBasicAuth(email, api_token)`, `body.storage` for raw HTML, page through one space | Content is **Confluence Storage Format** (XHTML + macros + `ac:`/`ri:` tags), not clean text, cleaning macros + resolving links is the real work; plus pagination + rate limits; Cloud v1 vs v2 differ | OAuth 2.0 (3LO), job queue + retry/backoff, multi-space |
| **Jira create-issue** | One HTTPS POST to the create-issue endpoint, same Atlassian token | Cloud v3 `description` must be **ADF JSON** (a plain string 400s); required-field config varies per project, pin to one known-good project | OAuth 2.0, retry/backoff, richer field mapping |

**Interfaces:** `KBSource.iter_pages() -> Doc(id, title, html, url)` · `Tracker.create_issue(title, body, project) -> url` · `ChatBot.on_mention(handler)` / `reply(space, text_or_card)`. Provider/credentials/base-URL come from config. **Re-index cost:** changing the KB source (or how storage-HTML is chunked/cleaned) requires re-ingest + re-embed; changing chat or tracker provider has **no** re-index cost (neither holds indexed state).

### 5.6 Webex bot, feasibility (verified against developer.webex.com)

**Verdict: fully feasible, and the simplest path needs no public URL.** Build a **Bot** (not an OAuth Integration): a bot has its own identity, lives in spaces, and replies as itself; an Integration acts on behalf of a human user: the wrong model for an autonomous assistant.

**Personal setup (build & test):**
- Create the bot at developer.webex.com → *My Webex Apps → Create a Bot* → copy the **bot access token** (long-lived; shown once). A normal/free Webex account is enough to create it.
- Run it locally with the **`webex-bot` Python library in websocket mode**. It opens an *outbound* websocket to the Webex cloud, so it needs **no public URL, no ngrok, no inbound firewall**. It also fetches the message text + card-button inputs for you (the raw API only hands you an id and forces a follow-up GET).
- In **group spaces the bot only receives a message when it is @mentioned** (Webex privacy rule), exactly KAI's `@KAI` model. In 1:1 spaces it sees all messages from that user. Strip the mention markup, then call `/ask`.
- Reply with `markdown`, or an **Adaptive Card** (one card per message) with `Action.Submit` buttons for 👍 / 👎 / Escalate; clicks arrive as a separate `attachmentActions:created` event.
- Watch-outs: ~300 req/min default rate limit (honor `429` + `Retry-After`), ~7.4 KB message cap, Adaptive Cards ≤ v1.3 / fixed 432px width / ~80 KB.

**Office / enterprise (later: no code rebuild):**
- The **same bot, same token, same APIs** run inside an office org. Moving there is a **policy/config/approval** step, **not a rewrite**.
- The one gate you can't control yourself: the target org's **Control Hub bot policy**. If bots are "Allowed" (a common default) any user can add KAI; if "Denied", IT must **allow-list the bot's `@webex.bot` email** (selective allow-listing requires the org's *Pro Pack* add-on).
- **Enterprise data stays enterprise:** a space is owned by the org of the first non-bot participant, so when KAI joins an office space the **org owns the conversation data** and its compliance/eDiscovery tooling keeps full visibility, your personal registrar account does not move data into your region.
- **Privacy story for the security review:** the @mention-only model means the bot is *structurally unable* to read group chatter it isn't mentioned in. (What KAI's backend does with the messages it *is* mentioned in is your responsibility, store/log carefully.)
- **Design-for-now so the office step is config-only:** build to @mention-only; don't hardcode space/org IDs (resolve via API); keep the bot token + any future webhook URL in config/env. For production you either keep the websocket worker or register an HTTPS webhook with signature verification, config, not code.
- If KAI ever outgrows @mention (org-wide reads, act-as-org), Webex **Service Apps** are the enterprise-native upgrade (Full-Admin authorization in Control Hub), more capability, more gating. Stay on the bot model while it suffices.

---

## 6. Capabilities: Know / Ask / Inform

### Know
Ingest Confluence (one or more spaces/instances) and local files → clean storage-format HTML to text → chunk (header- + title-aware) → embed → upsert into the hybrid vector store with metadata (doc id, title, URL, space). Re-ingest is incremental and idempotent (content-hash skip; stable chunk ids).

### Ask
`POST /ask {question}` → embed → **hybrid retrieve** (dense + full-text, RRF) top-N → cross-encoder rerank → build a tight, citation-instructed prompt → call the config-selected LLM → return **`{answer, citations[], confidence, escalated, escalation_url, suggested_sources[]}`**. The answer **must** cite the source pages it used; an answer with no supporting chunks is **not** emitted as a confident answer.

### Inform, **approval-gated. Never auto-publish.**
When a human answers an escalated/gap question well, that validated Q&A becomes new knowledge, but **only** through an explicit human-approval step (`/admin/inform` → approve), optionally 4-eyes, with 👎-driven auto-quarantine. KAI never writes back to the KB on its own.

---

## 7. Confidence & citations (brief)

- **Citations are mandatory.** Every confident answer lists the source page(s) (title + URL) for the chunks it used. No supporting chunk → no confident answer.
- **Confidence** is derived from retrieval strength, best cosine similarity blended with the cross-encoder relevance, scaled by the question's lexical coverage in the retrieved chunks. A heuristic, not a calibrated probability.
- **Never-fabricate is more than the threshold.** Beyond the confidence gate (`CONFIDENCE_THRESHOLD`, eval-calibrated 0.45), every confident answer passes a guard stack before it is emitted: an IDK/refusal detector; deterministic fabrication checks (invented qualified identifiers / config URIs, and significant numbers absent from the sources); a source-vocabulary-overlap floor (`ANSWER_GROUNDING_MIN`); an optional per-sentence grounding check; and an optional LLM verification pass (`VERIFY_ANSWERS`, on by default). Any failure escalates instead of answering.
- **Escalate when unsure:** below the threshold (or on any guard failure), KAI does **not** guess. It returns a "not confident" reply and opens a Jira issue (when configured) with the question + closest sources, returning the issue URL.
- Thresholds are **config**, tuned against the **golden-question eval set** (`eval/`); re-run `eval/run_eval.py` after any embedding/reranker/engine swap, since fusion + scoring shift.

---

## 8. Status & future scope

**Built (v1):** Confluence (multi-space, multi-instance, Cloud + Server/DC) and local files ingested together; `/ask` + `/ask-document` + `/search` + feedback/escalate + `/admin/*`; hybrid retrieval (dense + full-text, RRF) + cross-encoder rerank + confidence gate + grounding/verification guards; Jira escalation (local fallback); the Inform learn-back loop (approval-gated, 4-eyes optional, 👎 quarantine); web UI + Webex/Slack/Teams bots; model-agnostic via config.

**Future scope (not built):**
- **More sources** behind `KBSource`: a web/URL crawler (the general path for non-Confluence wikis like MediaWiki/DokuWiki), SharePoint, Google Drive, Git repos/wikis.
- **More trackers** behind `Tracker`: GitHub/GitLab issues, ServiceNow, Linear, plain email; richer paging/routing.
- **Enterprise hardening:** SSO, RBAC, multi-tenant isolation, per-user OAuth for Atlassian/Webex, job queues + retry/backoff, vector-store clustering, eval in CI.
- **Heavier RAG frameworks, only if the need arises:** LlamaIndex (multi-hop, many connectors), Haystack (typed/observable pipelines), or **LangChain / LangGraph** when KAI must become a tool-calling **agent** (see §5.3). KAI is deliberately framework-free today: a small, owned `retrieve → rerank → prompt → answer` pipeline, for the fewest dependencies and zero hidden prompt mutation.

See [roadmap.md](roadmap.md) for the tracked list.

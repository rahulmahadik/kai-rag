# KAI, Architecture & How It Works

**Author:** Rahul Mahadik

This document explains how KAI works internally: the ingest and answer
pipelines, the design choices behind "never give a confident wrong answer", and
the swappable provider boundaries. For setup and running, see
[`setup-and-run.md`](setup-and-run.md).

---

## Repository layout

```text
kai/                          # the Python package
├── app.py                    #   FastAPI app + all HTTP endpoints
├── bot.py                    #   chat-bot entrypoint (CHAT_PLATFORM picks the adapter)
├── config.py                 #   pydantic-settings config (every env var)
├── factory.py                #   builds providers from config (source, store, LLM, tracker)
├── interfaces.py             #   core dataclasses + Protocols
├── telemetry.py              #   Prometheus metrics
├── pipeline/                 # the RAG pipeline
│   ├── ingest.py             #   crawl → chunk → embed → store (incremental); reindex
│   ├── chunk.py              #   header/title-aware chunking
│   ├── ask.py                #   retrieve → answer → cite → escalate (the guards)
│   ├── verify.py             #   LLM grounding/verification pass
│   ├── multiquery.py         #   query reformulation   (+ rewrite.py, query rewrite)
│   ├── prompt.py             #   strict, citation-instructed prompt
│   └── inform.py             #   approval-gated curated-answer loop
├── chat/                     # one brain, many surfaces
│   ├── base.py               #   ChatAdapter protocol + message types
│   ├── service.py            #   ChatService (drives /ask + feedback) + format_reply
│   ├── slack.py              #   Slack (Socket Mode)
│   ├── webex.py              #   Webex (outbound websocket)
│   └── teams.py              #   Microsoft Teams (inbound webhook)
└── providers/                # swappable integrations behind Protocols
    ├── confluence_cloud.py   #   Confluence source (Cloud + Server/DC)
    ├── file_source.py        #   local files / PDF / Markdown / HTML
    ├── embedding_openai.py   #   OpenAI-compatible embeddings
    ├── llm_openai.py         #   OpenAI-compatible LLM
    ├── reranker.py           #   cross-encoder reranker (+ noop)
    ├── vectorstore_pgvector.py  # Postgres/pgvector store
    ├── jira_cloud.py         #   Jira escalation tracker
    └── local_tracker.py      #   local fallback tracker

frontend/                     # standalone web chat UI (one HTML file, no build step)
run/setup.py                  # install · start · ingest · reindex · ui · bot · doctor · fresh
eval/                         # golden eval + bot simulator (run_eval.py, simulate_bot.py, ...)
samples/                      # bundled ORIGINAL sample docs (01–05)
tests/                        # pytest suite
doc/                          # documentation (you are here)
deploy/                       # systemd unit + Slack app manifest
pyproject.toml                # packaging + optional extras: [bot] [slack] [teams] [rerank] [dev]
```

## 1. The big picture

KAI is a retrieval-augmented question answering system. It indexes a knowledge
base, and for each question it retrieves the most relevant passages, decides
whether it is confident enough to answer, and either writes a grounded, cited
answer or escalates to a human. It is built to run **self-hosted**: point the
LLM + embeddings at a local OpenAI-compatible server (e.g. Ollama) and use a
local Postgres/pgvector, and the cross-encoder reranker runs in-process, so your
documents and questions can stay on your own infrastructure. The provider
boundaries are OpenAI-compatible, so you can equally point them at a hosted
endpoint. Note the knowledge source (Confluence) and escalation tracker (Jira)
are reached over the network, and on escalation the question + closest sources
are written to the ticket (the unverified model draft only if
`ESCALATION_INCLUDE_DRAFT` is on).

```
INGEST   KBSource ─▶ chunk (header- + title-aware) ─▶ Embedder ─▶ VectorStore

ASK      question
            │
            ▼
   (optional) query rewrite ─▶ normalize (strip filler) ─▶ (optional) multi-query
            │
            ▼
   Embedder ─▶ VectorStore.search (dense + full-text, fused with RRF) ─▶ rerank
            │
            ▼
   confidence gate ── below threshold / nothing retrieved ──▶ escalate (Tracker)
            │
         cleared
            ▼
   grounded prompt ─▶ LLM ─▶ answer
            │
            ├─ IDK / refusal ─────────────────────────────────────▶ escalate
            ├─ grounding guards (invented specifics / numbers /
            │    too little source overlap / unsupported sentences) ▶ escalate
            ├─ verification pass (optional) fails ─────────────────▶ escalate
            ▼
   Answer { answer, citations[], confidence, escalated:false }
           (escalations carry escalation_url + suggested_sources[])
```

Every external dependency sits behind a small Protocol in
[`kai/interfaces.py`](../kai/interfaces.py), `Embedder`, `LLMClient`,
`VectorStore`, `KBSource`, `Tracker`. `kai/factory.py` constructs the real
implementation of each from config, so models and sources are swapped by
configuration, not code.

---

## 2. Ingest pipeline

`kai/pipeline/ingest.py` pulls every document from the `KBSource`, chunks it,
embeds the chunks, and upserts them into the vector store.

1. **Source**, `ConfluenceCloudKBSource` pages through a Confluence space (or a
   single page + its descendants when `CONFLUENCE_ROOT_PAGE` is set), yielding
   the raw storage-format HTML per page. Auth is optional (anonymous for public
   spaces, Basic auth for private).
2. **Chunking**, `kai/pipeline/chunk.py`:
   - cleans the storage HTML to text, turning headings into section markers;
   - **header-aware** splitting keeps each section coherent and carries its
     heading into every chunk;
   - **title-aware**: the page title is prepended to each chunk's text, so the
     title's distinctive terms ride into both the embedding and the lexical
     index. This is what stops a *"BloodHound proposal"* query from matching a
     near-identical *"Lucene proposal"* chunk: the bodies look alike, the titles
     disambiguate.
3. **Embed + store**, chunks are embedded in batches via the OpenAI-compatible
   `/embeddings` endpoint and upserted into Postgres + pgvector. Chunk ids are
   stable (`"{doc_id}#{ordinal}"`), so re-ingesting overwrites rather than
   duplicating.

Re-ingest when the source content changes, the embedding model changes (vectors
must be regenerated), or the chunking changes.

Re-ingest is **incremental and safe**: a per-document SHA-256 content hash lets an
unchanged page skip chunk+embed+upsert entirely, and each changed document is
replaced atomically (embed first, then delete-and-insert in one transaction) so an
embedder failure can never leave it half-written. A full crawl also **prunes** docs
removed upstream, guarded by a mass-delete refusal and a "seen but skipped" set so a
transient blip can't delete valid pages.

---

## 3. Retrieval

`VectorStore.search` (pgvector) does **hybrid** retrieval in one round-trip:

- **Dense**, cosine distance (`<=>`) against the query embedding (HNSW index);
- **Lexical**, `websearch_to_tsquery` full-text against a generated `tsvector`
  column (GIN index);
- the two ranked lists are fused with **Reciprocal Rank Fusion (RRF)**.

The store also returns each chunk's raw cosine similarity (`vector_score`), an
*absolute* relevance signal used later by the confidence gate (the RRF score is
rank-based and sits near the top even for an off-topic query, so it cannot tell
"answerable" from "off-topic"; cosine similarity can).

### Reranking

A **cross-encoder** (`sentence-transformers`, e.g. `ms-marco-MiniLM`) re-scores
each `(query, chunk)` pair jointly, far more precise than the bi-encoder cosine
used for first-stage retrieval. When reranking is on, KAI over-fetches a wider
candidate pool (`RERANK_CANDIDATES`), reranks it, and keeps `TOP_K`. This
promotes the right passage to the top (recall@k ≫ recall@1, so the right page is
usually retrieved: the reranker gets it to #1).

### Multi-query expansion (optional)

`kai/pipeline/multiquery.py`: the LLM produces a couple of reformulations of the
question (typos fixed, rephrased toward likely document terms). KAI retrieves a
pool for the original **and** each variant, unions the candidates, then reranks
the union against the **original** question. This recovers messy/typo/casual
questions without changing what the user asked. Cost: one short LLM call per
question.

---

## 4. The confidence gate: the heart of "never give wrong info"

Before any answer is generated, `kai/pipeline/ask.py::_confidence` scores how
well the question is actually covered by the retrieved passages:

- **Primary signal**: the best cosine similarity (`vector_score`) blended with
  the calibrated cross-encoder relevance (`sigmoid` of the top rerank logit).
  These are *absolute* relevance measures.
- **Secondary signal**, lexical coverage of the question's content words in the
  retrieved chunks.

`confidence = relevance × (0.5 + 0.5 × coverage)`, clamped to [0, 1].

If `confidence < CONFIDENCE_THRESHOLD` (or nothing was retrieved), KAI
**escalates without calling the LLM**. The decision is retrieval-based, so the
model's output would be discarded anyway. This is what makes an out-of-scope
question ("how do I reset my VPN?") escalate instead of being answered from a
weakly-related passage.

The threshold is calibrated empirically against a labelled question set so that
out-of-scope questions fall below it while in-scope questions clear it.

---

## 5. Answer generation + grounding guards

When the gate clears, `kai/pipeline/prompt.py` builds a strict, citation-instructed
prompt: the retrieved passages are numbered sources, and the model must

- use **only** facts in the context (no outside knowledge),
- answer about the **exact subject** the question names, if the sources are
  about a *different* but similar item, say "I don't know" rather than answer
  about the wrong one,
- cite each claim as `[n]`, and
- answer thoroughly and only say "I don't know" when nothing is relevant
  (never mix a real answer with "I don't know").

After generation, several guards run:

- **IDK detection**: a refusal (the model's "I don't know", or an uncited reply
  containing a refusal phrase) escalates.
- **Trailing-IDK strip**: a contradictory "I don't know" appended after a real
  answer is removed.
- **Deterministic grounding guards** (no LLM, always on), escalate if the answer
  states a concrete *specific* (a dotted class/identifier or config URI) absent
  from every source, or a *significant number* not found in any source, or draws
  too little of its vocabulary from the sources (`ANSWER_GROUNDING_MIN`). An
  optional per-sentence semantic check (`SENTENCE_GROUNDING`) catches recombination
  fabrication the bag-of-words check misses.
- **Citations**, only the sources the model actually cited (`[n]`) are returned,
  de-duplicated by URL and renumbered 1:1 with the Sources list; any model-emitted
  "Sources:" block is stripped (the UI/chat render the real list).
- **Verification pass (`VERIFY_ANSWERS`, on by default)**: a second LLM check that
  the answer is supported by its sources and about the right subject; if not, KAI
  escalates. Strongest guard against the confusable-document failure, at the cost
  of one extra LLM call; it **fails open** (a verifier error doesn't block a good
  answer). Most effective with a strong general LLM as the judge.

---

## 6. Escalation

When KAI cannot answer confidently, `Tracker.create_issue` records the
escalation. With Jira configured, it opens a real ticket (description rendered as
Atlassian Document Format) and the answer links to it. With no tracker
configured, `LocalTracker` logs the escalation and the user sees a "flagged for a
human to review" message with **no** link, honest, no fake URL. The closest
retrieved sources are recorded for whoever picks it up.

---

## 7. Provider boundaries (swappable)

| Boundary      | Real implementation                                        |
| ------------- | ---------------------------------------------------------- |
| `Embedder`    | OpenAI-compatible `/embeddings`                            |
| `LLMClient`   | OpenAI-compatible `/chat/completions`                      |
| `VectorStore` | Postgres + pgvector (hybrid: dense + full-text, fused with RRF) |
| Reranker      | cross-encoder via `sentence-transformers`                 |
| `KBSource`    | Confluence (Cloud or public/anonymous), local files (PDF / md / txt / html), or both |
| `Tracker`     | Jira Cloud REST v3; `LocalTracker` when unconfigured      |

`build_providers` is real-only and **fails loudly** if a required value is blank.
Adding a new knowledge source (SharePoint, Notion, a folder of docs, a website)
is a single new provider implementing `KBSource.iter_pages`: no pipeline change.
Swapping the LLM or embedder is a config change (an embedder change requires a
re-ingest, since vector dimensions change).

---

## 8. Serving surfaces

- **HTTP API** (`kai/app.py`), `POST /ask` (grounded answer or escalation),
  `POST /ask-document` (ad-hoc Q&A over an uploaded file, never stored),
  `POST /search` (retrieve-only), `POST /feedback`, `POST /escalate`,
  `POST /notify`, `GET /metrics`, `GET /health`, and maintainer routes under
  `/admin/*` (the Inform loop `POST /admin/inform[/{id}/approve|reject|revoke]` +
  `GET /admin/inform`, `GET /admin/gaps`, `POST /admin/reindex`). Optional bearer
  auth (`KAI_API_KEY`) on every route except `/health`; when a key is set the
  interactive docs + `/openapi.json` are hidden too. CORS is deny-by-default. A
  generic 500 handler hides internals; the reranker is pre-warmed at startup.
- **Chat bots** (`kai/chat/`, run via `python -m kai.bot`, selected by
  `CHAT_PLATFORM`), **Webex** and **Slack** open an **outbound** websocket (no
  public URL); **Teams** serves an inbound Bot Framework webhook. All are thin
  clients over the same `ChatService` → `/ask`. Webex has per-domain/per-user
  access control and inbound file Q&A.
- **Web UI** (`frontend/`): a standalone, dependency-free browser app that calls
  `/ask` (and `/ask-document` for the 📎 upload) over CORS.

---

## 9. Why each guard exists

| Failure mode | Guard |
| ------------ | ----- |
| Answering an off-topic question from a weak match | cosine + cross-encoder **confidence gate** (escalate before the LLM runs) |
| Answering about the wrong but similar document | **title-aware chunking** (retrieval) + **subject-match** prompt rule + optional **verification pass** |
| Messy/typo questions missing the right page | **multi-query expansion** |
| Model hedging / refusing | **IDK detection** + **trailing-IDK strip** |
| Hallucinated citations | return only the `[n]` the model actually cited |
| Stale / fake escalation links | `LocalTracker` (no fake URL) |

The through-line: **prefer escalation over a confident wrong answer.** Every
layer is tuned so that when KAI does answer, it is grounded and cited, and when
it isn't sure, it says so.

---

## 10. Inform, learning from escalations (approval-gated)

The **I** in KAI. Questions KAI couldn't answer become a review queue, and a
human-approved answer is curated back into the knowledge base, **never
auto-published** (`kai/pipeline/inform.py`, the `/admin/inform*` endpoints):

1. **Gaps**, every `/ask` is recorded by `kai/telemetry.py`; `GET /admin/gaps`
   surfaces the most-escalated questions (the content-gap backlog).
2. **Submit**: a maintainer posts an answer for a gap (`POST /admin/inform`); it
   lands as **pending**, indexed nowhere yet.
3. **Approve**, `POST /admin/inform/{id}/approve` synthesizes the Q+A into a
   curated `Doc` (`space="kai-curated"`), pushed through the same chunk→embed→upsert
   path. Indexing happens **only** here. Optional 4-eyes (`INFORM_REQUIRE_SEPARATE_APPROVER`)
   requires the approver to differ from the author; on approval the original asker
   is DM'd. A curated answer is labelled as community-reviewed when served.
4. **Self-correction**, 👍/👎 (`POST /feedback`) is recorded; enough 👎 on a curated
   answer auto-**quarantines** it (un-indexed for re-review). `reject`/`revoke`
   drop or pull an entry.

Supporting machinery: an in-process **answer cache** (exact-match on the normalized
question, busted on every ingest/reindex/curation) and Prometheus-style counters at
`GET /metrics`.

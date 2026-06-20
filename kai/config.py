"""KAI runtime configuration.

A single ``Settings`` object (pydantic-settings, pydantic v2) reads from the
process environment and an optional ``.env`` file. Defaults are deliberately
permissive empty strings / sensible numbers so that *importing* settings never
fails — the real providers are responsible for validating that the specific
values they need are present and raising loudly when they are blank.

Field names here are part of the fixed contract: ``kai.factory`` and the real
provider modules read these attributes directly.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration loaded from env + ``.env``."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ------------------------------------------------------------------
    # LLM (OpenAI-compatible chat completions)
    # ------------------------------------------------------------------
    llm_base_url: str = ""  # OpenAI-compatible chat URL (Ollama: http://localhost:11434/v1)
    llm_api_key: str = ""  # Bearer token for the LLM endpoint ("" for local/no-auth servers)
    llm_model: str = ""  # chat model name, e.g. "llama3.1", "qwen2.5", "gpt-4o-mini"
    llm_timeout: int = 120  # seconds to wait for one completion before giving up

    # ------------------------------------------------------------------
    # Embeddings (OpenAI-compatible)
    # ------------------------------------------------------------------
    embed_base_url: str = ""  # OpenAI-compatible /embeddings URL (often the LLM host)
    embed_api_key: str = ""  # Bearer token for the embeddings endpoint ("" for local servers)
    embed_model: str = ""  # embedding model, e.g. "nomic-embed-text", "text-embedding-3-small"
    embed_dimensions: int = 768  # embedding vector width — must match the model
    # Task-instruction prefixes prepended to text before embedding. Some models
    # (notably nomic-embed-text) are TRAINED to require an asymmetric prefix —
    # "search_query: " on questions and "search_document: " on passages — and
    # retrieval quality degrades materially without them. Left blank for models
    # that don't use prefixes; auto-filled for nomic-embed* in ``model_post_init``.
    # Queries and passages MUST be embedded with their matching prefix, so changing
    # either value requires a re-ingest to stay consistent.
    embed_query_prefix: str = ""
    embed_passage_prefix: str = ""

    # ------------------------------------------------------------------
    # Database (Postgres + pgvector)
    # ------------------------------------------------------------------
    database_url: str = ""  # Postgres DSN: postgresql://user:pass@host:5432/kai
    vector_table: str = "kai_chunks"  # embedded-chunks table (hashes: <name>_hashes)
    # Stored vector precision — switchable:
    #   "vector"  = float32 (exact, pgvector default)
    #   "halfvec" = fp16 (2x smaller; measured LOSSLESS here — recall@10 = 1.000).
    # halfvec only matters at large scale (millions of chunks); at small scale it
    # changes nothing measurable. Changing this requires a re-ingest (the embedding
    # column type changes). Validated in PgVectorStore.
    vector_type: str = "vector"

    # ------------------------------------------------------------------
    # Knowledge source selection
    #   "confluence"        — a Confluence space (default)
    #   "files"             — local files under SOURCE_DIR (PDF / md / txt / html)
    #   "confluence+files"  — both, ingested together (alias: "both")
    # ------------------------------------------------------------------
    source_type: str = "confluence"
    source_dir: str = ""  # directory of files to ingest when source_type includes files
    # SOURCE_DIRS: comma-separated list of directories (overrides SOURCE_DIR) so you
    # can ingest several folders at once. Multiple Confluence instances are set via
    # numbered env vars (CONFLUENCE_2_BASE_URL, …) — see doc/sources.md.
    source_dirs: str = ""
    # Skip files larger than this many bytes (0 = no limit). A SIZE cap, not a page
    # cap: an image-heavy PDF can be huge with few pages while a long text PDF is
    # small, so bytes — not page count — is the right guard against OOM on ingest.
    file_max_bytes: int = 25_000_000  # ~25 MB

    # ------------------------------------------------------------------
    # Confluence Cloud (knowledge base)
    # ------------------------------------------------------------------
    confluence_base_url: str = ""  # https://you.atlassian.net/wiki (Cloud) or Server/DC base
    confluence_email: str = ""  # account email — pair with a token for Basic auth (Cloud)
    confluence_api_token: str = ""  # API token (Cloud) or PAT (Server/DC); blank = anonymous
    confluence_space_key: str = ""  # space to ingest, e.g. "ENG" (more: CONFLUENCE_2_*)
    # 0 = ingest the whole space; >0 caps how many pages are ingested (handy for
    # trying a large public space without pulling thousands of pages).
    confluence_max_docs: int = 0
    # Optional: scope ingestion to ONE page + ALL its descendant pages (a subtree)
    # instead of the whole space. Accepts a page ID or an exact page title. Blank
    # = ingest the whole space.
    confluence_root_page: str = ""

    # ------------------------------------------------------------------
    # Jira Cloud (escalation tracker)
    # ------------------------------------------------------------------
    jira_base_url: str = ""  # https://you.atlassian.net — blank disables escalation tickets
    jira_email: str = ""  # account email for Jira Basic auth
    jira_api_token: str = ""  # Jira API token
    jira_project_key: str = ""  # project escalation tickets are filed in, e.g. "SUP"
    jira_issue_type: str = "Task"  # issue type to create, e.g. "Task" / "Bug"
    # Data-egress boundary: when escalating to an EXTERNAL tracker, the
    # ticket body always carries the question + closest sources; the MODEL DRAFT
    # (unverified generated text) is included only when this is on. Default OFF —
    # residency-sensitive deploys shouldn't ship unverified model output to a
    # third-party cloud. Note the question itself still egresses on escalation;
    # that is documented behavior an operator accepts by configuring Jira.
    escalation_include_draft: bool = False

    # ------------------------------------------------------------------
    # Webex bot (Phase 1) — a thin websocket client over the /ask API.
    # ``webex_bot_token`` is the *bot* access token from developer.webex.com;
    # ``kai_api_url`` points the bot at a running KAI API (it POSTs /ask there).
    # ------------------------------------------------------------------
    webex_bot_token: str = Field(default="", validation_alias="WEBEX_BOT_TOKEN")
    kai_api_url: str = Field(default="http://127.0.0.1:8100", validation_alias="KAI_API_URL")
    # Restrict who the bot will answer (comma-separated). Blank = anyone sharing
    # the space (fine for a personal test; SET before exposing private content).
    webex_approved_domains: str = Field(default="", validation_alias="WEBEX_APPROVED_DOMAINS")
    webex_approved_users: str = Field(default="", validation_alias="WEBEX_APPROVED_USERS")
    # Show the 👍/👎/escalate-anyway Adaptive Card under confident answers. OFF by
    # default so demo answers stay clean; turn on to collect feedback. When on,
    # clicking a button dismisses the card and posts a short acknowledgement.
    webex_feedback_card: bool = Field(default=False, validation_alias="WEBEX_FEEDBACK_CARD")

    # Chat platform selector for the bot process: "webex" (default) | "slack" |
    # "teams" (inbound webhook). Each is a thin client over the same /ask API.
    # Plain fields (no validation_alias): case_sensitive=False already maps the
    # CHAT_PLATFORM / SLACK_* env vars, and this keeps them settable by kwarg too.
    chat_platform: str = "webex"
    slack_bot_token: str = ""  # CHAT_PLATFORM=slack — Socket Mode needs both tokens
    slack_app_token: str = ""
    slack_feedback_buttons: bool = True
    # CHAT_PLATFORM=teams — Azure Bot Service registration (pip install '.[teams]').
    # Teams is INBOUND: it serves POST /api/messages, so it needs a public HTTPS URL.
    teams_app_id: str = ""
    teams_app_password: str = ""
    teams_app_tenant_id: str = ""  # blank = multi-tenant (botframework.com)
    teams_host: str = "0.0.0.0"
    teams_port: int = 3978
    # Edit the "searching…" ack message into the answer in place (no delete/repost
    # churn) instead of delete-then-post. Falls back to a normal reply if the
    # direct Webex REST edit fails. Webex only.
    webex_edit_in_place: bool = True
    # Customisable bot copy (markdown). BOT_ACK_MESSAGE is the instant "thinking"
    # message; BOT_ANSWER_PREFIX is bolded above the answer when editing in place
    # (set it blank for no prefix).
    bot_ack_message: str = "🔎 _Searching the knowledge base…_"
    bot_answer_prefix: str = "Here's what I found:"
    # Per-thread conversation memory (EXPERIMENTAL, default OFF): prepend the last
    # question's topic to a clearly-referential follow-up ("what about X?") so it
    # retrieves in context. Never bypasses the gate/guards (the enriched query
    # still runs the full pipeline); needs live chat testing before enabling.
    conversation_memory: bool = False

    # ------------------------------------------------------------------
    # API auth — shared-secret bearer required on /ask, /search, /ingest when
    # set. Empty = no auth (local dev). MUST be set before exposing KAI over a
    # network or public URL, or the whole corpus is readable by anyone with it.
    # ------------------------------------------------------------------
    api_key: str = Field(default="", validation_alias="KAI_API_KEY")

    # Comma-separated CORS origins allowed to call the API from a browser. The web
    # frontend is a SEPARATE origin (see frontend/), so it needs this. Secure by
    # default: EMPTY means no cross-origin browser access is granted. Set it to your
    # frontend's exact URL(s); "*" works but warns loudly (never use it with an
    # unauthenticated, internet-exposed API).
    cors_origins: str = ""

    # ------------------------------------------------------------------
    # Retrieval / answering
    # ------------------------------------------------------------------
    # Answer cache: exact-match on the normalized question, busted on every
    # /ingest. Confident answers only. 0 disables.
    answer_cache_size: int = 256
    # Telemetry: store the raw question text in kai_questions (needed for the
    # /admin/gaps aggregation). Set false to store only the SHA-256 (PII-sensitive
    # deploys).
    telemetry_question_text: bool = True

    # Level for KAI's own loggers (kai.*) — uvicorn doesn't configure them, so the
    # app wires a handler at this level (see kai.app._configure_logging). INFO shows
    # the per-ingest summary + file-skip/escalation notices; DEBUG adds detail.
    log_level: str = "INFO"

    top_k: int = 8
    # Escalate (don't answer) below this confidence. 0.45 is calibrated against the
    # golden question set (eval/golden.json): in-scope questions clear it, out-of-scope
    # fall below — biased toward escalating when unsure. Re-tune with eval/run_eval.py
    # for your corpus. Keep config.py, .env and .env.example aligned on this value.
    confidence_threshold: float = 0.45
    # "noop" (off) | "cross_encoder" (rerank the candidates with a cross-encoder).
    reranker: str = "noop"
    reranker_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    # Whether the reranker model emits a 0-1 probability (sigmoid head, e.g.
    # bge-reranker-v2-m3) rather than a raw logit (ms-marco). Probabilistic scores
    # are used directly as relevance; logits are passed through a sigmoid. Wrong
    # value miscalibrates the confidence gate, so it pairs with reranker_model.
    rerank_score_is_probability: bool = False
    # When the reranker is on, retrieve this many candidates first, rerank them,
    # then keep top_k. A wider pool gives the reranker more to promote from.
    rerank_candidates: int = 20
    # When true, the LLM normalises a messy question (fixes spelling/grammar,
    # meaning preserved) BEFORE retrieval — improves robustness to typos. Adds
    # one short LLM call per query. See kai.pipeline.rewrite.
    # Strip conversational/imperative lead-ins ("show me details of X" -> "X") for
    # RETRIEVAL + RERANK so the cross-encoder scores the topic, not the filler. The
    # user's original question is still used for the answer + ticket. On by default.
    normalize_query: bool = True
    query_rewrite: bool = False
    # Second-pass answer verification (grounding + subject match) — the strongest
    # guard against false info: escalates a generated answer that isn't fully
    # supported by its sources or is about the wrong subject. One extra short LLM
    # call per answered question. See kai.pipeline.verify.
    verify_answers: bool = True
    # Multi-query expansion: the LLM generates reformulations of the question,
    # retrieve for each, union the candidates, then rerank against the original.
    # Recovers messy/typo/casual questions. Adds one short LLM call per query.
    multi_query: bool = False
    multi_query_count: int = 2

    # ------------------------------------------------------------------
    # Chunking — MUST stay within the EMBEDDING model's context window.
    # nomic-embed-text accepts 8192 tokens, but mxbai-embed-large only 512, so a
    # short-context embedder needs a smaller target (set CHUNK_TARGET_TOKENS).
    # ------------------------------------------------------------------
    chunk_target_tokens: int = 500
    chunk_overlap_tokens: int = 60

    # Deterministic anti-fabrication guard: an answer must draw at least this
    # fraction of its meaningful words from the retrieved sources, else it is
    # treated as fabricated (pulled from the model's own knowledge) and escalated.
    # Calibrated on this corpus (eval): fabricated how-to answers overlap the
    # sources at <=0.38, grounded answers at >=0.57 — so 0.48 (the midpoint)
    # separates them with margin on both sides. Set to 0 to disable. Generic — it
    # diffs the answer against the retrieved text, with no per-corpus knowledge.
    answer_grounding_min: float = 0.48

    # Inform loop safeguards against a WRONG human-curated answer being served
    # forever. inform_require_separate_approver: the approver must differ from the
    # submitter (4-eyes). inform_downvote_quarantine: after this many 👎 on a
    # curated answer it is auto-un-indexed and flagged for re-review (0 disables).
    inform_require_separate_approver: bool = False
    inform_downvote_quarantine: int = 3

    # Sentence-level grounding (EXPERIMENTAL — default OFF): every prose
    # sentence of a confident answer must be semantically supported by at least
    # one retrieved chunk (embedding cosine >= sentence_grounding_min), else the
    # answer escalates. Catches recombination fabrication that bag-of-words
    # overlap misses. Enable only after validating against the golden eval —
    # an over-strict floor causes false escalations of good answers.
    sentence_grounding: bool = False
    sentence_grounding_min: float = 0.55

    # Token budget for the generated answer. Thorough multi-source answers can run
    # ~900 tokens; 1536 leaves margin so a complete answer is never truncated
    # mid-sentence (which would also drop its closing citation).
    answer_max_tokens: int = 1536

    def model_post_init(self, __context: object) -> None:
        """Fill model-specific embedding prefixes when the operator left them blank.

        nomic-embed-text mandates ``search_query:`` / ``search_document:`` task
        prefixes; without them dense retrieval is run off-distribution. We default
        them ONLY when both are blank (so an explicit choice is never overridden)
        and the embedding model is a nomic-embed variant. Any other model stays
        unprefixed unless the operator sets the prefixes explicitly.
        """

        if not self.embed_query_prefix and not self.embed_passage_prefix:
            model = (self.embed_model or "").strip().lower()
            if model.startswith("nomic-embed"):
                self.embed_query_prefix = "search_query: "
                self.embed_passage_prefix = "search_document: "


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached ``Settings`` instance built from the environment."""

    return Settings()

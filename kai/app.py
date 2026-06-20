"""KAI HTTP API (FastAPI).

Exposes the pipeline over three endpoints:

* ``GET  /health``  → liveness;
* ``POST /ingest``  → (re)ingest the knowledge base, returns the chunk count;
* ``POST /ask``     → answer a question, returns the full :class:`Answer`
  (including citations and any escalation URL).

Providers are built ONCE at startup from :class:`~kai.config.Settings` and reused
across requests. (The Postgres store opens a fresh connection per operation; add
a connection pool if you need higher throughput.) Run it with::

    uvicorn kai.app:app
"""

from __future__ import annotations

import hmac
import logging
import threading
import time
from dataclasses import asdict

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from kai.config import Settings, get_settings
from kai.factory import build_providers
from kai.interfaces import Answer
from kai.pipeline.ask import ask as ask_pipeline
from kai.pipeline.ingest import ingest as ingest_pipeline

logger = logging.getLogger("kai")


def _prewarm_reranker(settings: Settings) -> None:
    """Load the cross-encoder in a BACKGROUND thread so startup (and ``/health``)
    is never blocked by the first-time model download (~90s on a fresh install).
    No-op when the reranker is off. The model loads behind a lock, so a query that
    arrives mid-load simply shares the same instance once it's ready.
    """

    if (settings.reranker or "noop").strip().lower() == "noop":
        return

    def _load() -> None:
        try:
            from kai.providers.reranker import _get_model

            _get_model(settings.reranker_model)
            logger.info("kai_reranker_prewarmed model=%s", settings.reranker_model)
        except Exception as exc:  # don't crash startup; a query will surface it
            logger.warning("kai_reranker_prewarm_failed err=%s", type(exc).__name__)

    import threading

    threading.Thread(target=_load, name="reranker-prewarm", daemon=True).start()


class AskRequest(BaseModel):
    """Body for ``POST /ask``."""

    question: str = Field(..., min_length=1, max_length=2000, description="The user's question.")

    @field_validator("question")
    @classmethod
    def _non_blank(cls, v: str) -> str:
        # min_length=1 admits whitespace-only ("   "), which the pipeline then
        # rejects with a ValueError → HTTP 500. Strip-and-reject here so a blank
        # question returns a clean 422 instead.
        if not v or not v.strip():
            raise ValueError("question must not be blank")
        return v


class IngestResponse(BaseModel):
    """Body for ``POST /ingest``."""

    ingested: int = Field(..., description="Number of chunks written to the store.")


class FeedbackRequest(BaseModel):
    """Body for ``POST /feedback`` — 👍/👎 from the chat surface."""

    question: str = Field(..., min_length=1, max_length=2000)
    verdict: str = Field(..., pattern="^(up|down)$")
    reporter: str = Field(default="", max_length=200)


class EscalateRequest(BaseModel):
    """Body for ``POST /escalate`` — the user's explicit "escalate anyway".

    Does NOT re-run /ask: it files a ticket for the stated question directly
    (the human override for an over-confident answer).
    """

    question: str = Field(..., min_length=1, max_length=2000)
    reporter: str = Field(default="", max_length=200)


class AskDocumentRequest(BaseModel):
    """Body for ``POST /ask-document`` — ad-hoc Q&A over an uploaded file.

    The file bytes are base64-encoded (JSON, no multipart dependency); KAI extracts
    the text, runs in-memory RAG scoped to JUST this document, and answers with the
    same never-fabricate guards. Nothing is written to the corpus."""

    question: str = Field(..., min_length=1, max_length=2000)
    filename: str = Field(..., min_length=1, max_length=400)
    # Cap the base64 body so an oversized upload is rejected at validation (422)
    # BEFORE it's buffered + decoded. ~40 MB b64 ≈ a 30 MB file; FILE_MAX_BYTES is
    # still enforced on the decoded bytes.
    content_b64: str = Field(..., min_length=1, max_length=40_000_000)


class InformRequest(BaseModel):
    """Body for ``POST /admin/inform`` — a human's curated answer to a gap.

    Queued as PENDING; nothing is indexed until ``/approve``."""

    question: str = Field(..., min_length=3, max_length=2000)
    answer: str = Field(..., min_length=1, max_length=20000)
    author: str = Field(default="", max_length=200)
    asker: str = Field(default="", max_length=320, description="original asker — DMed on approval")


class ApproveRequest(BaseModel):
    """Body for approve — who approves (for the dual-control / audit trail)."""

    approver: str = Field(default="", max_length=200)


class NotifyRequest(BaseModel):
    """Body for ``POST /notify`` — proactively DM a Webex user (e.g. when an
    escalation is resolved, or from the future Inform loop)."""

    email: str = Field(..., min_length=3, max_length=320)
    message: str = Field(..., min_length=1, max_length=7000)


class HealthResponse(BaseModel):
    """Body for ``GET /health``."""

    status: str


def _configure_logging(settings: Settings) -> None:
    """Make KAI's own loggers (``kai.*``) surface in the server log.

    uvicorn configures only its own loggers, so without this the per-ingest summary,
    file-skip warnings and escalation notices are silently dropped. We attach one
    dedicated stderr handler (captured into the server log file) to the ``kai``
    parent logger at the configured level, with ``propagate=False`` so nothing is
    double-logged. Idempotent — safe across repeated ``create_app`` calls.
    """

    level = getattr(logging, str(getattr(settings, "log_level", "INFO")).upper(), logging.INFO)
    kai_logger = logging.getLogger("kai")
    kai_logger.setLevel(level)
    if not any(getattr(h, "_kai_handler", False) for h in kai_logger.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s", "%H:%M:%S")
        )
        handler._kai_handler = True  # type: ignore[attr-defined]
        kai_logger.addHandler(handler)
    kai_logger.propagate = False


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the FastAPI app, wiring providers once from ``settings``."""

    settings = settings or get_settings()
    _configure_logging(settings)  # before anything that might log
    providers = build_providers(settings)
    _prewarm_reranker(settings)  # move the ~90s cold-start to boot, not 1st query

    from kai.telemetry import Telemetry

    telemetry = Telemetry(
        settings.database_url,
        store_question_text=getattr(settings, "telemetry_question_text", True),
    )

    from kai.pipeline.inform import InformStore

    inform = InformStore(settings.database_url)

    # Answer cache: exact-match on the normalized question, BUSTED on every
    # /ingest (the load-bearing part — a cache that survives a KB update serves
    # stale answers). Confident answers only; escalations always re-run so a
    # repeat question still files/refreshes its ticket. In-process, bounded.
    from collections import OrderedDict

    answer_cache: OrderedDict[str, dict] = OrderedDict()
    cache_size = int(getattr(settings, "answer_cache_size", 256) or 0)
    _cache_lock = threading.Lock()  # OrderedDict ops aren't atomic across the threadpool

    def _bust_cache() -> None:
        with _cache_lock:
            answer_cache.clear()

    def _cache_key(q: str) -> str:
        return " ".join(q.lower().split())

    # Hide interactive docs + schema once an API key is set (production posture) so
    # anonymous scanners can't enumerate the /admin/* surface via /openapi.json;
    # keep them on for local dev (no key set).
    _keyed = bool(settings.api_key)
    _description = (
        "**KAI — Know · Ask · Inform.**\n\n"
        "A self-hosted, grounded RAG assistant: it answers questions from *your* "
        "documents (Confluence + files) **with citations**, and **escalates instead of "
        "guessing** — KAI never fabricates.\n\n"
        "- `POST /ask` — grounded answer + citations, or an escalation to a human.\n"
        "- `POST /ask-document` — ad-hoc Q&A over an uploaded file (read once, never stored).\n"
        "- `POST /search` — retrieve-only (no LLM): top chunks + scores.\n"
        "- `POST /feedback` · `POST /escalate` — 👍/👎 and raise-for-a-human.\n"
        "- `/admin/*` — ingest, the Inform curation loop, and reindex (maintainers).\n"
    )
    _tags_meta = [
        {
            "name": "Ask & search",
            "description": "Ask questions and retrieve — grounded and cited, or escalated.",
        },
        {
            "name": "Knowledge base",
            "description": "Build and maintain the index from your sources.",
        },
        {
            "name": "Feedback & escalation",
            "description": "👍/👎, raise-for-a-human, and proactive notifications.",
        },
        {
            "name": "Learning loop (Inform)",
            "description": "Approval-gated curated answers + escalation gaps.",
        },
        {"name": "Ops", "description": "Liveness and metrics."},
    ]
    app = FastAPI(
        title="KAI — Know · Ask · Inform",
        description=_description,
        version="1.0.0",
        openapi_tags=_tags_meta,
        contact={"name": "Rahul Mahadik", "url": "https://rahulmahadik.com"},
        license_info={
            "name": "MIT",
            "url": "https://github.com/rahulmahadik/kai-rag/blob/main/LICENSE",
        },
        docs_url=None if _keyed else "/docs",
        redoc_url=None if _keyed else "/redoc",
        openapi_url=None if _keyed else "/openapi.json",
    )
    # CORS — the web frontend is a SEPARATE app/origin (see frontend/). Allow ONLY
    # the origins in CORS_ORIGINS (comma-separated). Secure by default: when unset,
    # NO cross-origin browser access is granted — and we never silently wildcard.
    _cors = [o.strip() for o in (settings.cors_origins or "").split(",") if o.strip()]
    if "*" in _cors:
        logger.warning(
            "CORS_ORIGINS='*' lets ANY website call this API from a browser; set it "
            "to your frontend's exact URL before exposing KAI beyond localhost."
        )
    if _cors:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=_cors,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    # Loud warning when the corpus is unauthenticated — easy to miss until exposed.
    if not settings.api_key:
        logger.warning(
            "KAI_API_KEY is not set — the API is UNAUTHENTICATED and the whole "
            "corpus is readable by anyone who can reach it. Set KAI_API_KEY before "
            "exposing KAI beyond localhost."
        )

    # Stash on app state so handlers (and tests) can reach them.
    app.state.settings = settings
    app.state.providers = providers

    def _require_api_key(authorization: str | None = Header(default=None)) -> None:
        """when ``api_key`` is configured, require a matching bearer token.
        No-op when unset (local dev). ``/health`` is intentionally left open."""
        if not settings.api_key:
            return
        # Constant-time compare so the bearer check can't be timing-probed.
        if not hmac.compare_digest(authorization or "", f"Bearer {settings.api_key}"):
            raise HTTPException(status_code=401, detail="Invalid or missing API key.")

    @app.exception_handler(Exception)
    async def _on_unhandled(request, exc):  # noqa: ANN001,ARG001 — FastAPI handler
        # log the real error server-side; return a generic message so we never
        # leak base URLs / space keys / project keys to the caller.
        logger.error("kai_unhandled path=%s err=%s", request.url.path, repr(exc))
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal error — please retry or contact an administrator."},
        )

    @app.get("/health", response_model=HealthResponse, tags=["Ops"], summary="Liveness check")
    def health() -> HealthResponse:
        return HealthResponse(status="ok")

    @app.get("/", include_in_schema=False)
    def home() -> dict:  # API banner — the web chat UI is a separate app (frontend/)
        banner = {
            "name": "KAI",
            "status": "ok",
            "docs": None if _keyed else "/docs",  # docs are hidden in keyed/prod mode
            "health": "/health",
            "ui": "served separately — see the frontend/ directory",
        }
        # Self-describing in open/dev mode; in keyed/prod mode don't enumerate the API
        # surface to unauthenticated callers (the banner itself stays open for liveness).
        if not _keyed:
            banner["endpoints"] = {
                "ask": "POST /ask",
                "ask_document": "POST /ask-document",
                "search": "POST /search",
                "feedback": "POST /feedback",
                "escalate": "POST /escalate",
                "health": "GET /health",
            }
        return banner

    @app.post(
        "/ingest",
        response_model=IngestResponse,
        tags=["Knowledge base"],
        summary="(Re)build the index — incremental (unchanged docs skipped)",
        dependencies=[Depends(_require_api_key)],
    )
    def ingest() -> IngestResponse:
        # Prune (remove docs deleted upstream) ONLY for a full whole-space crawl:
        # a capped or subtree crawl does not see every live page, so pruning there
        # would wrongly delete valid documents.
        full_crawl = not settings.confluence_root_page and not settings.confluence_max_docs
        count = ingest_pipeline(
            providers,
            target_tokens=settings.chunk_target_tokens,
            overlap_tokens=settings.chunk_overlap_tokens,
            passage_prefix=settings.embed_passage_prefix,
            prune=full_crawl,
        )
        _bust_cache()  # the KB changed — cached answers may now be stale
        return IngestResponse(ingested=count)

    @app.post(
        "/ask",
        tags=["Ask & search"],
        summary="Ask a question → grounded, cited answer (or an escalation, never a guess)",
        dependencies=[Depends(_require_api_key)],
    )
    def ask(req: AskRequest) -> dict:
        key = _cache_key(req.question)
        cached = None
        if cache_size:
            with _cache_lock:  # the cache touch is locked; telemetry/return aren't
                cached = answer_cache.get(key)
                if cached is not None:
                    answer_cache.move_to_end(key)
        if cached is not None:
            telemetry.record_cache_hit()  # counts the ask + a cache hit, no latency skew
            return cached

        start = time.monotonic()
        answer = ask_pipeline(req.question, providers, settings)
        duration_ms = int((time.monotonic() - start) * 1000)
        # one structured event per ask — this is what makes the confidence
        # gate measurable in production (escalation rate, calibration, latency).
        telemetry.record_ask(
            req.question,
            confidence=answer.confidence,
            escalated=answer.escalated,
            citation_count=len(answer.citations),
            escalation_url=answer.escalation_url,
            duration_ms=duration_ms,
        )
        # The Answer dataclass (with nested Citation dataclasses) serialises
        # cleanly via asdict → JSON.
        payload = asdict(answer)
        if cache_size and not answer.escalated:
            with _cache_lock:
                answer_cache[key] = payload
                while len(answer_cache) > cache_size:
                    answer_cache.popitem(last=False)
        return payload

    @app.post(
        "/ask-document",
        tags=["Ask & search"],
        summary="Ad-hoc Q&A over an uploaded file (read once, never stored)",
        dependencies=[Depends(_require_api_key)],
    )
    def ask_document(req: AskDocumentRequest) -> dict:
        """Answer a question grounded ONLY in an uploaded file (ad-hoc RAG).

        Decodes the file, extracts text, and runs in-memory retrieval + the full
        gate/guards scoped to this document. No corpus writes, no Jira ticket.
        """

        import base64

        from kai.pipeline.ask import answer_from_document
        from kai.providers.file_source import (
            content_type_for,
            extract_text,
            is_unsupported_upload,
            unreadable_reason,
        )

        def _unreadable() -> dict:
            return asdict(
                Answer(
                    answer=unreadable_reason(req.filename),
                    citations=[],
                    confidence=0.0,
                    escalated=True,
                )
            )

        # Reject a known-binary type (office/image/archive) up front with a clear
        # "unsupported format" message — before buffering/decoding ~30 MB of base64.
        if is_unsupported_upload(req.filename):
            return _unreadable()
        try:
            data = base64.b64decode(req.content_b64, validate=True)
        except Exception:  # noqa: BLE001
            raise HTTPException(status_code=422, detail="content_b64 is not valid base64.")
        cap = getattr(settings, "file_max_bytes", 0) or 0
        if cap and len(data) > cap:
            raise HTTPException(status_code=413, detail=f"File exceeds FILE_MAX_BYTES ({cap}).")
        text = extract_text(req.filename, data)
        if not text.strip():
            return _unreadable()
        embedder, llm, _store, _kb, _tracker = providers
        start = time.monotonic()
        answer = answer_from_document(
            req.question,
            text,
            req.filename,
            embedder,
            llm,
            settings,
            content_type=content_type_for(req.filename),
        )
        telemetry.record_ask(
            f"[doc:{req.filename}] {req.question}",
            confidence=answer.confidence,
            escalated=answer.escalated,
            citation_count=len(answer.citations),
            escalation_url=None,
            duration_ms=int((time.monotonic() - start) * 1000),
        )
        return asdict(answer)

    @app.post(
        "/feedback",
        tags=["Feedback & escalation"],
        summary="Record 👍/👎 on an answer",
        dependencies=[Depends(_require_api_key)],
    )
    def feedback(req: FeedbackRequest) -> dict:
        """persist 👍/👎. A 👎 on a CURATED answer is the self-correction
        signal — enough of them auto-un-index it for re-review, so a wrong curated
        answer can't keep being served."""

        telemetry.record_feedback(req.question, req.verdict, req.reporter)
        out = {"status": "recorded"}
        if req.verdict == "down":
            # The quarantine side-effects touch the DB; a failure here must NEVER 500
            # the feedback call (the 👎 was already recorded) — degrade to a log.
            try:
                hits = inform.downvote_curated(req.question)
                threshold = getattr(settings, "inform_downvote_quarantine", 0) or 0
                quarantined = []
                for hit in hits:
                    if threshold and hit["downvotes"] >= threshold:
                        _unindex_curated(hit["id"])
                        inform.mark(hit["id"], "quarantined")
                        logger.warning(
                            "kai_inform_quarantined id=%s downvotes=%d", hit["id"], hit["downvotes"]
                        )
                        quarantined.append(hit["id"])
                if quarantined:
                    out["quarantined"] = quarantined
            except Exception as exc:  # noqa: BLE001 — never break /feedback
                logger.error("kai_feedback_quarantine_failed err=%s", type(exc).__name__)
        return out

    @app.post(
        "/escalate",
        tags=["Feedback & escalation"],
        summary="Escalate a question to a human (files a tracker ticket)",
        dependencies=[Depends(_require_api_key)],
    )
    def escalate(req: EscalateRequest) -> dict:
        """explicit "escalate anyway" — file a ticket WITHOUT re-running /ask."""

        _embedder, _llm, _store, _kb, tracker = providers
        telemetry.record_feedback(req.question, "escalate", req.reporter)
        title = "KAI escalation (user-requested): " + " ".join(req.question.split())
        body = (
            "A user explicitly asked for a human follow-up via the chat surface.\n\n"
            f"Question: {req.question}\n"
            f"Requested by: {req.reporter or '(unknown)'}"
        )
        try:
            url = (tracker.create_issue(title=title, body=body) or "").strip()
        except Exception as exc:  # noqa: BLE001 — degrade, never 500 the button
            logger.error("kai_escalation_failed err=%s", type(exc).__name__)
            url = ""
        return {"status": "escalated", "escalation_url": url or None}

    @app.post(
        "/notify",
        tags=["Feedback & escalation"],
        summary="Proactively DM a Webex user (e.g. when an escalation is resolved)",
        dependencies=[Depends(_require_api_key)],
    )
    def notify(req: NotifyRequest) -> dict:
        """Proactively DM a user on the active chat platform (Webex).

        Closes the loop: when an escalation is answered (by a human, or the future
        Inform loop), notify the original asker directly. Uses WEBEX_BOT_TOKEN; the
        recipient must have messaged the bot before (Webex 1:1 rule).
        """

        token = (settings.webex_bot_token or "").strip()
        if not token:
            raise HTTPException(status_code=400, detail="WEBEX_BOT_TOKEN is not configured.")
        if "@" not in (req.email or ""):
            raise HTTPException(status_code=422, detail="email must be a valid address.")
        from kai.chat.webex import send_direct_message

        sent = send_direct_message(token, req.email, req.message)
        if not sent:
            # Don't return 200 on a failed send — the caller can't tell it didn't land.
            raise HTTPException(
                status_code=502,
                detail="Could not deliver the notification (recipient may not have messaged the bot).",
            )
        return {"status": "sent"}

    @app.get(
        "/metrics",
        tags=["Ops"],
        summary="Prometheus metrics (asks, escalation rate, cache hits, latency)",
        dependencies=[Depends(_require_api_key)],
    )
    def metrics():  # noqa: ANN202 — plain text response
        """Prometheus text exposition (in-process counters; reset on restart)."""

        from fastapi.responses import PlainTextResponse

        return PlainTextResponse(telemetry.metrics_text())

    @app.get(
        "/admin/gaps",
        tags=["Learning loop (Inform)"],
        summary="Most-escalated unanswered questions (where to curate next)",
        dependencies=[Depends(_require_api_key)],
    )
    def gaps(limit: int = 50) -> dict:
        """most-escalated questions — which pages to write next."""

        return {"gaps": telemetry.gaps(limit=max(1, min(limit, 500)))}

    # --- Inform loop: gaps → human answer → approval → curated KB ---
    @app.post(
        "/admin/inform",
        tags=["Learning loop (Inform)"],
        summary="Submit a curated answer to a gap (queued PENDING until approved)",
        dependencies=[Depends(_require_api_key)],
    )
    def inform_submit(req: InformRequest) -> dict:
        """Queue a curated answer for a gap question (PENDING — not indexed yet)."""

        cid = inform.submit(req.question, req.answer, req.author, req.asker)
        return {"id": cid, "status": "pending"}

    _INFORM_STATUSES = {"pending", "approved", "rejected", "revoked", "quarantined"}

    @app.get(
        "/admin/inform",
        tags=["Learning loop (Inform)"],
        summary="List curated-answer candidates by status (pending/approved/…)",
        dependencies=[Depends(_require_api_key)],
    )
    def inform_list(status: str = "pending", limit: int = 100, offset: int = 0) -> dict:
        """List candidate answers (default the pending review queue; ``all`` for every).

        ``limit`` (≤500) + ``offset`` page through older candidates than the first 100.
        """

        if status not in _INFORM_STATUSES and status != "all":
            raise HTTPException(
                status_code=422,
                detail=f"status must be one of {sorted(_INFORM_STATUSES)} or 'all'.",
            )
        return {
            "candidates": inform.list(
                status="" if status == "all" else status,
                limit=max(1, min(limit, 500)),
                offset=max(0, offset),
            )
        }

    def _unindex_curated(candidate_id: int) -> None:
        embedder, _llm, store, _kb, _tracker = providers
        try:
            store.delete(f"kai-curated:{candidate_id}")
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "kai_inform_unindex_failed id=%s err=%s", candidate_id, type(exc).__name__
            )
        _bust_cache()

    @app.post(
        "/admin/inform/{candidate_id}/approve",
        tags=["Learning loop (Inform)"],
        summary="Approve a candidate → embed it into the curated knowledge base",
        dependencies=[Depends(_require_api_key)],
    )
    def inform_approve(candidate_id: int, req: ApproveRequest | None = None) -> dict:
        """Approve → synthesize the Q+A into a curated KB doc and index it.

        Indexing happens ONLY here (never on submit). Optional 4-eyes: with
        INFORM_REQUIRE_SEPARATE_APPROVER, the approver must differ from the author.
        """

        from kai.pipeline.inform import index_curated_answer

        cand = inform.get(candidate_id)
        if cand is None:
            raise HTTPException(status_code=404, detail="candidate not found")
        if cand["status"] == "approved":
            return {"id": candidate_id, "status": "approved", "chunks": cand.get("chunks", 0)}
        approver = (req.approver if req else "") or ""
        if getattr(settings, "inform_require_separate_approver", False):
            if not approver.strip():
                raise HTTPException(
                    status_code=400, detail="approver is required (4-eyes is enabled)."
                )
            if approver.strip().lower() == (cand.get("author") or "").strip().lower():
                raise HTTPException(
                    status_code=403, detail="approver must differ from the author (4-eyes)."
                )
        embedder, _llm, store, _kb, _tracker = providers
        chunks = index_curated_answer(
            cand["question"],
            cand["answer"],
            embedder,
            store,
            candidate_id=candidate_id,
            target_tokens=settings.chunk_target_tokens,
            overlap_tokens=settings.chunk_overlap_tokens,
            passage_prefix=settings.embed_passage_prefix,
        )
        # downvotes=0: re-approving a quarantined answer starts its 👎 count fresh, so
        # one new downvote can't instantly re-quarantine it.
        inform.mark(candidate_id, "approved", chunks=chunks, approver=approver, downvotes=0)
        _bust_cache()  # a new curated answer may supersede a cached escalation
        # Close the loop: DM the original asker that their question now has an answer.
        notified = False
        asker = (cand.get("asker") or "").strip()
        token = (settings.webex_bot_token or "").strip()
        if asker and token:
            from kai.chat.webex import send_direct_message

            notified = send_direct_message(
                token,
                asker,
                f"Good news — your question **{cand['question']}** now has an answer in KAI. "
                "Ask me again and I'll share it.",
            )
        return {"id": candidate_id, "status": "approved", "chunks": chunks, "notified": notified}

    @app.post(
        "/admin/inform/{candidate_id}/reject",
        tags=["Learning loop (Inform)"],
        summary="Reject a pending candidate (won't be indexed)",
        dependencies=[Depends(_require_api_key)],
    )
    def inform_reject(candidate_id: int) -> dict:
        """Reject a PENDING candidate — dropped, never indexed."""

        cand = inform.get(candidate_id)
        if cand is None:
            raise HTTPException(status_code=404, detail="candidate not found")
        if cand["status"] == "approved":
            # An approved answer is INDEXED — rejecting wouldn't un-index it; use revoke.
            raise HTTPException(
                status_code=409, detail="candidate is approved/indexed — use /revoke to un-index."
            )
        if cand["status"] in ("rejected", "revoked"):
            return {"id": candidate_id, "status": cand["status"]}  # idempotent
        inform.mark(candidate_id, "rejected")
        return {"id": candidate_id, "status": "rejected"}

    @app.post(
        "/admin/inform/{candidate_id}/revoke",
        tags=["Learning loop (Inform)"],
        summary="Revoke an approved (indexed) answer — remove it from the corpus",
        dependencies=[Depends(_require_api_key)],
    )
    def inform_revoke(candidate_id: int) -> dict:
        """Pull an already-approved curated answer — UN-INDEX it in one call.

        The escape hatch for a WRONG answer that slipped through approval: it stops
        being retrievable immediately.
        """

        cand = inform.get(candidate_id)
        if cand is None:
            raise HTTPException(status_code=404, detail="candidate not found")
        if cand["status"] != "approved":
            # Only an approved answer is indexed; revoking anything else is a mistake.
            raise HTTPException(
                status_code=409,
                detail=f"can only revoke an approved candidate (status is {cand['status']!r}).",
            )
        _unindex_curated(candidate_id)
        inform.mark(candidate_id, "revoked")
        return {"id": candidate_id, "status": "revoked"}

    @app.post(
        "/admin/reindex",
        tags=["Knowledge base"],
        summary="Re-embed the whole corpus in place (after a model/chunking change)",
        dependencies=[Depends(_require_api_key)],
    )
    def reindex_endpoint() -> dict:
        """Rebuild the vector index in place — the clean alternative to reset-db.

        Re-embeds every source (clears the content hashes so nothing is skipped) and
        re-indexes approved curated answers, while keeping the Inform queue, feedback
        and telemetry. Per-doc replace (embed before delete) + the prune guards mean a
        failed/empty crawl can't wipe the corpus; an embedding-DIMENSION change is
        refused (it can't be applied in place — use reset-db). Prunes only on a full
        crawl. reset-db (drop the whole DB) stays available for a total wipe.
        """

        from kai.pipeline.ingest import reindex as reindex_pipeline

        # Prune only on a FULL whole-corpus crawl — a capped/subtree crawl can't see
        # every page, so pruning it would delete valid docs (same guard as /ingest).
        full_crawl = not settings.confluence_root_page and not settings.confluence_max_docs
        result = reindex_pipeline(
            providers,
            target_tokens=settings.chunk_target_tokens,
            overlap_tokens=settings.chunk_overlap_tokens,
            passage_prefix=settings.embed_passage_prefix,
            prune=full_crawl,
            inform_store=inform,
        )
        _bust_cache()  # the whole index changed — drop stale cached answers
        return result

    @app.post(
        "/search",
        tags=["Ask & search"],
        summary="Retrieve-only: ranked chunks + scores + the escalate decision (no LLM)",
        dependencies=[Depends(_require_api_key)],
    )
    def search(req: AskRequest) -> dict:
        """Retrieve-only (no LLM): top chunks + scores + the confidence/escalate
        decision. Uses the SAME retrieval path as /ask (rewrite + multi-query +
        rerank) so evaluation/tuning reflects production, not a weaker single
        query."""
        from kai.pipeline.ask import _confidence, retrieve

        embedder, llm, store, _kb, _tracker = providers
        q, scored = retrieve(req.question, embedder, llm, store, settings)
        confidence = _confidence(
            q,
            scored,
            rerank_is_prob=getattr(settings, "rerank_score_is_probability", False),
        )
        return {
            "query": q,
            "confidence": confidence,
            "escalate": confidence < settings.confidence_threshold,
            "results": [
                {
                    "title": sc.chunk.title,
                    "url": sc.chunk.url,
                    "score": sc.score,
                    "vector_score": sc.vector_score,
                }
                for sc in scored
            ],
        }

    return app


# Module-level ASGI app for ``uvicorn kai.app:app``. Built eagerly so uvicorn (and
# tooling) import a ready app. Guarded so that merely *importing* kai.app — for the
# test suite, CI, or `python -c "import kai.app"` — never requires a configured
# ``.env``. If config is missing/invalid we still expose a minimal app that returns
# a clear 503 on every route instead of crashing at import time.
try:
    app = create_app()
except Exception as _config_error:  # pragma: no cover - only without a valid .env
    _startup_error = _config_error
    app = FastAPI(title="KAI (unconfigured)")

    @app.get("/health")
    def _unconfigured_health() -> dict:  # noqa: D401 - tiny fallback handler
        return {"status": "unconfigured", "detail": str(_startup_error)}

    @app.api_route("/{_full_path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    def _unconfigured(_full_path: str):  # noqa: ANN202 - tiny fallback handler
        raise HTTPException(
            status_code=503,
            detail=(
                "KAI is not configured. Set EMBED_BASE_URL and EMBED_MODEL (plus your "
                "LLM/database settings) in a .env file, then restart. See README.md."
            ),
        )

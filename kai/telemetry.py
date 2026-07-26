"""Per-ask observability  + feedback persistence + /metrics.

One small, dependency-light module:

* ``record_ask``: one structured log line + one ``kai_questions`` row per
  ``/ask`` (confidence, escalated, citations, latency). This is what makes the
  confidence gate MEASURABLE in production, without it the core "never wrong"
  control is unobservable.
* ``record_feedback``, persists 👍/👎/escalate-anyway into ``kai_feedback``
  (the human signal the accuracy loop consumes).
* ``metrics_text``, Prometheus text exposition of in-process counters (no
  prometheus_client dependency).

All DB writes are BEST-EFFORT: a telemetry failure is logged and never breaks
the request that triggered it. The question text is stored raw by default
(local-first deploy, needed for the /admin/gaps aggregation); set
``TELEMETRY_QUESTION_TEXT=false`` to store only the SHA-256.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from collections import defaultdict

logger = logging.getLogger("kai.telemetry")


class Telemetry:
    """Counters + best-effort Postgres persistence for asks and feedback."""

    def __init__(self, database_url: str, *, store_question_text: bool = True) -> None:
        self._db = database_url
        self._raw_text = store_question_text
        self._lock = threading.Lock()
        self._counters: dict[str, float] = defaultdict(float)
        self._schema_ready = False

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _connect(self):  # noqa: ANN202 - psycopg connection
        import psycopg

        return psycopg.connect(self._db)

    def _ensure_schema(self, conn) -> None:  # noqa: ANN001
        if self._schema_ready:
            return
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS kai_questions (
                    id              bigserial PRIMARY KEY,
                    asked_at        timestamptz NOT NULL DEFAULT now(),
                    question_sha256 text NOT NULL,
                    question        text NOT NULL DEFAULT '',
                    confidence      real NOT NULL,
                    escalated       boolean NOT NULL,
                    citation_count  integer NOT NULL DEFAULT 0,
                    escalation_url  text NOT NULL DEFAULT '',
                    duration_ms     integer NOT NULL DEFAULT 0
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS kai_feedback (
                    id         bigserial PRIMARY KEY,
                    created_at timestamptz NOT NULL DEFAULT now(),
                    question   text NOT NULL,
                    verdict    text NOT NULL,
                    reporter   text NOT NULL DEFAULT ''
                )
                """
            )
        conn.commit()
        self._schema_ready = True

    def _bump(self, name: str, amount: float = 1.0) -> None:
        with self._lock:
            self._counters[name] += amount

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------
    def record_cache_hit(self) -> None:
        """A /ask served from the answer cache: count it as an ask + a cache hit, but
        do NOT touch the latency average (no compute happened: a 0ms entry would skew
        it) or write a duplicate question row (it was recorded when first answered)."""

        self._bump("kai_asks_total")
        self._bump("kai_ask_cache_hits_total")

    def record_ask(
        self,
        question: str,
        *,
        confidence: float,
        escalated: bool,
        citation_count: int,
        escalation_url: str | None,
        duration_ms: int,
    ) -> None:
        """One structured event per /ask: log line + counters + DB row."""

        qhash = hashlib.sha256((question or "").encode("utf-8", "replace")).hexdigest()
        logger.info(
            "kai_ask qhash=%s confidence=%.3f escalated=%s citations=%d duration_ms=%d",
            qhash[:16],
            confidence,
            escalated,
            citation_count,
            duration_ms,
        )
        self._bump("kai_asks_total")
        if escalated:
            self._bump("kai_escalations_total")
        self._bump("kai_ask_duration_ms_sum", float(duration_ms))
        self._bump("kai_ask_duration_ms_count")

        try:
            with self._connect() as conn:
                self._ensure_schema(conn)
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO kai_questions
                            (question_sha256, question, confidence, escalated,
                             citation_count, escalation_url, duration_ms)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            qhash,
                            question if self._raw_text else "",
                            float(confidence),
                            bool(escalated),
                            int(citation_count),
                            escalation_url or "",
                            int(duration_ms),
                        ),
                    )
                conn.commit()
        except Exception as exc:  # noqa: BLE001 - telemetry never breaks a request
            logger.warning("kai_telemetry_write_failed err=%s", type(exc).__name__)

    def record_feedback(self, question: str, verdict: str, reporter: str) -> None:
        self._bump(f"kai_feedback_{verdict}_total")
        try:
            with self._connect() as conn:
                self._ensure_schema(conn)
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO kai_feedback (question, verdict, reporter) "
                        "VALUES (%s, %s, %s)",
                        (question[:1000], verdict, reporter[:200]),
                    )
                conn.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("kai_feedback_write_failed err=%s", type(exc).__name__)

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------
    def metrics_text(self) -> str:
        """Prometheus text exposition of the in-process counters."""

        with self._lock:
            items = sorted(self._counters.items())
        lines = ["# KAI in-process counters (reset on restart)"]
        for name, value in items:
            lines.append(f"{name} {value:g}")
        return "\n".join(lines) + "\n"

    def gaps(self, limit: int = 50) -> list[dict]:
        """Most-escalated questions (normalized): the knowledge-gap signal."""

        try:
            with self._connect() as conn:
                self._ensure_schema(conn)
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT lower(regexp_replace(question, '\\s+', ' ', 'g')) AS q,
                               count(*) AS n,
                               max(asked_at) AS last_asked,
                               avg(confidence) AS avg_confidence
                        FROM kai_questions
                        WHERE escalated AND question <> ''
                        GROUP BY q
                        ORDER BY n DESC, last_asked DESC
                        LIMIT %s
                        """,
                        (limit,),
                    )
                    return [
                        {
                            "question": r[0],
                            "count": r[1],
                            "last_asked": r[2].isoformat() if r[2] else "",
                            "avg_confidence": float(r[3] or 0.0),
                        }
                        for r in cur.fetchall()
                    ]
        except Exception as exc:  # noqa: BLE001
            logger.warning("kai_gaps_query_failed err=%s", type(exc).__name__)
            return []

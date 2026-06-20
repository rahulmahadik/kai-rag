"""Inform loop  — turn gaps into curated knowledge, approval-gated.

The flow that makes KAI *learn*:

  1. A question escalates (no confident answer) or gets a 👎 — surfaced by
     ``/admin/gaps`` (telemetry).
  2. A human SUBMITS an answer for that question → it lands in ``kai_kb_candidates``
     as **pending**. Nothing is indexed yet.
  3. An approver calls **approve** → the Q+A is synthesized into a curated
     :class:`~kai.interfaces.Doc` (``space='kai-curated'``) and pushed through the
     SAME chunk→embed→upsert path as any source. Now the next identical question
     retrieves it and clears the gate.
  4. **reject** drops it, un-indexed.

The load-bearing rule is structural: indexing happens ONLY in ``approve`` — the
submit path never writes to the vector store. So KAI can never auto-publish an
unreviewed answer.

``index_curated_answer`` (the synthesis+index step) is pure orchestration over the
provider Protocols and is unit-tested with fakes; ``InformStore`` is the Postgres
candidate queue.
"""

from __future__ import annotations

import logging

from kai.interfaces import Doc, Embedder, VectorStore
from kai.pipeline.chunk import chunk_document
from kai.pipeline.ingest import _embed_chunks

logger = logging.getLogger("kai.inform")

CURATED_SPACE = "kai-curated"


def index_curated_answer(
    question: str,
    answer: str,
    embedder: Embedder,
    store: VectorStore,
    *,
    candidate_id: int,
    target_tokens: int = 500,
    overlap_tokens: int = 60,
    passage_prefix: str = "",
    url: str = "",
) -> int:
    """Synthesize a curated Q→A into the vector store. Returns chunks written.

    The question becomes the doc TITLE (so the same question retrieves it strongly
    on both the dense and lexical arms) and the human answer is the body. Stable
    ``doc_id`` (``kai-curated:{id}``) so re-approval overwrites rather than dupes.
    Embed-before-delete: a failure can't wipe a prior version.
    """

    if not (answer or "").strip():
        return 0  # a curated entry must carry a real answer — never index a bare question
    doc = Doc(
        id=f"kai-curated:{candidate_id}",
        title=question.strip(),
        url=url,
        html=f"{question.strip()}\n\n{answer.strip()}",
        space=CURATED_SPACE,
        content_type="text",  # human answer — never HTML-mangled
    )
    store.ensure_schema(embedder.dimensions)
    chunks = chunk_document(doc, target_tokens=target_tokens, overlap_tokens=overlap_tokens)
    if not chunks:
        return 0
    vectors = _embed_chunks(embedder, chunks, passage_prefix)
    store.delete(doc.id)
    store.upsert(chunks, vectors)
    logger.info("kai_inform_indexed candidate=%s chunks=%d", candidate_id, len(chunks))
    return len(chunks)


class InformStore:
    """Postgres-backed approval queue for curated-answer candidates."""

    def __init__(self, database_url: str) -> None:
        self._db = database_url
        self._ready = False

    def _connect(self):  # noqa: ANN202
        import psycopg

        return psycopg.connect(self._db)

    def _ensure(self, conn) -> None:  # noqa: ANN001
        if self._ready:
            return
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS kai_kb_candidates (
                    id          bigserial PRIMARY KEY,
                    created_at  timestamptz NOT NULL DEFAULT now(),
                    updated_at  timestamptz NOT NULL DEFAULT now(),
                    question    text NOT NULL,
                    answer      text NOT NULL,
                    author      text NOT NULL DEFAULT '',
                    asker       text NOT NULL DEFAULT '',
                    approver    text NOT NULL DEFAULT '',
                    status      text NOT NULL DEFAULT 'pending',
                    chunks      integer NOT NULL DEFAULT 0,
                    downvotes   integer NOT NULL DEFAULT 0
                )
                """
            )
            # Backfill columns if the table predates these safeguards.
            cur.execute(
                "ALTER TABLE kai_kb_candidates ADD COLUMN IF NOT EXISTS asker text NOT NULL DEFAULT ''"
            )
            cur.execute(
                "ALTER TABLE kai_kb_candidates ADD COLUMN IF NOT EXISTS approver text NOT NULL DEFAULT ''"
            )
            cur.execute(
                "ALTER TABLE kai_kb_candidates ADD COLUMN IF NOT EXISTS downvotes integer NOT NULL DEFAULT 0"
            )
        conn.commit()
        self._ready = True

    def submit(self, question: str, answer: str, author: str = "", asker: str = "") -> int:
        """Queue a candidate (pending). Does NOT index — approval does that.

        ``asker`` is the original asker's address (optional) — DMed on approval."""

        with self._connect() as conn:
            self._ensure(conn)
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO kai_kb_candidates (question, answer, author, asker) "
                    "VALUES (%s, %s, %s, %s) RETURNING id",
                    (question.strip(), answer.strip(), author[:200], asker[:320]),
                )
                new_id = cur.fetchone()[0]
            conn.commit()
        logger.info("kai_inform_submitted candidate=%s author=%r", new_id, author[:60])
        return new_id

    _COLS = [
        "id",
        "created_at",
        "question",
        "answer",
        "author",
        "asker",
        "approver",
        "status",
        "chunks",
        "downvotes",
    ]

    def _row_to_dict(self, row) -> dict:  # noqa: ANN001
        return {
            c: (v.isoformat() if c == "created_at" and v else v) for c, v in zip(self._COLS, row)
        }

    def list(self, status: str = "pending", limit: int = 100, offset: int = 0) -> list[dict]:
        # ``offset`` lets a caller page through ALL candidates (the per-page cap stays
        # at 500) — used by reindex so curated answers beyond the first page aren't lost.
        with self._connect() as conn:
            self._ensure(conn)
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {', '.join(self._COLS)} FROM kai_kb_candidates "
                    "WHERE (%s = '' OR status = %s) "
                    "ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s",
                    (status, status, max(1, min(limit, 500)), max(0, offset)),
                )
                return [self._row_to_dict(r) for r in cur.fetchall()]

    def get(self, candidate_id: int) -> dict | None:
        # Direct lookup by id — NOT a scan of list(limit=500), so a candidate older
        # than the 500 most-recent can still be approved/rejected/revoked.
        with self._connect() as conn:
            self._ensure(conn)
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {', '.join(self._COLS)} FROM kai_kb_candidates WHERE id=%s",
                    (candidate_id,),
                )
                row = cur.fetchone()
        return self._row_to_dict(row) if row else None

    def mark(
        self,
        candidate_id: int,
        status: str,
        chunks: int | None = None,
        approver: str | None = None,
        downvotes: int | None = None,
    ) -> None:
        sets, params = ["status=%s", "updated_at=now()"], [status]
        if chunks is not None:
            sets.append("chunks=%s")
            params.append(chunks)
        if approver is not None:
            sets.append("approver=%s")
            params.append(approver[:200])
        if downvotes is not None:  # reset on (re)approval so old 👎 don't re-quarantine
            sets.append("downvotes=%s")
            params.append(downvotes)
        params.append(candidate_id)
        with self._connect() as conn:
            self._ensure(conn)
            with conn.cursor() as cur:
                cur.execute(f"UPDATE kai_kb_candidates SET {', '.join(sets)} WHERE id=%s", params)
            conn.commit()

    def downvote_curated(self, question: str) -> list[dict]:
        """+1 the downvotes of EVERY approved curated answer matching ``question`` and
        return ``[{id, downvotes}, …]``. The self-correction signal: enough 👎 and the
        caller quarantines it. Match is normalized (case/whitespace-insensitive). All
        matches are returned so two approved answers to the same question both count."""

        norm = "lower(regexp_replace({c}, '\\s+', ' ', 'g'))"
        try:
            with self._connect() as conn:
                self._ensure(conn)
                with conn.cursor() as cur:
                    cur.execute(
                        f"UPDATE kai_kb_candidates SET downvotes = downvotes + 1, updated_at=now() "
                        f"WHERE status='approved' AND {norm.format(c='question')} "
                        f"= {norm.format(c='%s')} RETURNING id, downvotes",
                        (question,),
                    )
                    rows = cur.fetchall()
                conn.commit()
        except Exception as exc:  # noqa: BLE001 — must never break /feedback
            logger.warning("kai_inform_downvote_failed err=%s", type(exc).__name__)
            return []
        return [{"id": r[0], "downvotes": r[1]} for r in rows]

"""Postgres + pgvector implementation of :class:`kai.interfaces.VectorStore`.

This module persists chunk vectors in Postgres using the ``pgvector`` extension
and retrieves the most relevant chunks by *fusing* two independent rankings:

* dense vector similarity (cosine distance KNN via the ``<=>`` operator), and
* lexical relevance (Postgres full-text search via ``websearch_to_tsquery`` over
  a stored ``tsvector`` column).

The two ranked lists are combined with **Reciprocal Rank Fusion (RRF)** so the
final ordering benefits from both semantic and keyword signals without needing
the two raw scores to be on a comparable scale.

Heavy SDKs (``psycopg`` v3, ``pgvector``) are imported at *module* level here on
purpose: importing this module implies the caller wants the pgvector backend.
"""

from __future__ import annotations

import re
from typing import Any, Sequence

import psycopg
from pgvector.psycopg import register_vector
from psycopg import sql
from psycopg.rows import dict_row

from kai.interfaces import Chunk, ScoredChunk

# Reciprocal Rank Fusion smoothing constant. 60 is the value from the original
# Cormack et al. RRF paper and is a sensible, widely-used default.
_RRF_K = 60

# Columns we allow callers to constrain via ``filters``. Restricting the set
# keeps the dynamic SQL safe (identifiers are never taken from arbitrary keys)
# and matches the metadata actually stored on each row.
_FILTERABLE_COLUMNS: frozenset[str] = frozenset({"doc_id", "space", "title", "url"})


class PgVectorStore:
    """A :class:`kai.interfaces.VectorStore` backed by Postgres + pgvector."""

    # Supported stored vector types (switchable). "vector" = float32 (exact);
    # "halfvec" = fp16 (2x smaller, ~lossless). Restricting the set keeps the
    # value safe to inline into DDL (column type + index opclass).
    _VECTOR_TYPES: frozenset[str] = frozenset({"vector", "halfvec"})

    def __init__(
        self,
        database_url: str,
        table: str = "kai_chunks",
        vector_type: str = "vector",
    ) -> None:
        if not database_url:
            raise ValueError(
                "PgVectorStore requires a non-empty database_url "
                "(set KAI database_url / DATABASE_URL)."
            )
        if not table or not table.replace("_", "").isalnum():
            raise ValueError(
                f"PgVectorStore requires a simple alphanumeric table name, got {table!r}."
            )
        vector_type = (vector_type or "vector").strip().lower()
        if vector_type not in self._VECTOR_TYPES:
            raise ValueError(
                f"Unsupported vector_type {vector_type!r}; use one of {sorted(self._VECTOR_TYPES)}."
            )
        self._database_url = database_url
        self._table = table
        self._vtype = vector_type  # validated -> safe to inline into DDL
        # Discovered on ensure_schema / first connect; used to validate upsert
        # vector widths. ``None`` until the schema is created or inspected.
        self._dimensions: int | None = None

    def _wrap(self, vec: list) -> object:
        """Wrap a float list in the right pgvector type for the configured store.

        halfvec values are passed as :class:`pgvector.HalfVector` so psycopg adapts
        them to the fp16 column; float32 stays a plain list (pgvector's default
        adaptation). Keeps the read/write paths identical apart from precision.
        """

        if self._vtype == "halfvec":
            from pgvector import HalfVector

            return HalfVector(vec)
        return vec

    # ------------------------------------------------------------------
    # Connection helper
    # ------------------------------------------------------------------
    def _connect(self) -> psycopg.Connection:
        """Open a new connection with pgvector type adaptation registered."""

        conn = psycopg.connect(self._database_url, autocommit=False)
        try:
            register_vector(conn)
        except Exception:
            # ``register_vector`` needs the extension to already exist. During
            # the very first ensure_schema() the extension may be absent; in
            # that case we register again after creating it. We must not leak
            # the connection if registration unexpectedly fails for any other
            # reason.
            pass
        return conn

    def _table_ident(self) -> sql.Identifier:
        return sql.Identifier(self._table)

    def _discover_schema(self) -> None:
        """Read the stored embedding column's type+width from the DB (idempotent).

        Sets ``self._dimensions`` so the query-side width guard works even when the
        process never called :meth:`ensure_schema`, and raises if the stored type
        disagrees with the configured ``vector_type`` (a re-ingest is required).
        No-op when the table doesn't exist yet.
        """

        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
                    "WHERE attrelid = to_regclass(%s) AND attname = 'embedding'",
                    (self._table,),
                )
                row = cur.fetchone()
        if not row or not row[0]:
            return  # table absent — ensure_schema will create it on ingest
        ftype = row[0]  # e.g. "vector(768)" / "halfvec(768)"
        name = ftype.split("(", 1)[0].strip().lower()
        if name != self._vtype:
            raise ValueError(
                f"Table {self._table!r} embedding column is {name!r} but "
                f"VECTOR_TYPE={self._vtype!r}. Re-ingest into a fresh table: "
                f"DROP TABLE {self._table}; then ingest again."
            )
        m = re.search(r"\((\d+)\)", ftype)
        if m:
            self._dimensions = int(m.group(1))

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------
    def ensure_schema(self, dimensions: int) -> None:
        """Create the pgvector extension, the chunk table and its indexes.

        Idempotent: safe to call repeatedly. The table carries the vector, the
        full-text ``tsvector`` (generated from title + text) and the chunk
        metadata used both for citations and for optional search filters.
        """

        if dimensions <= 0:
            raise ValueError(f"dimensions must be a positive integer, got {dimensions}.")
        self._dimensions = dimensions

        table = self._table_ident()
        create_table = sql.SQL(
            """
            CREATE TABLE IF NOT EXISTS {table} (
                id        text PRIMARY KEY,
                doc_id    text NOT NULL,
                title     text NOT NULL DEFAULT '',
                url       text NOT NULL DEFAULT '',
                space     text NOT NULL DEFAULT '',
                ordinal   integer NOT NULL DEFAULT 0,
                text      text NOT NULL DEFAULT '',
                embedding {vtype}({dim}) NOT NULL,
                ts        tsvector GENERATED ALWAYS AS (
                              to_tsvector(
                                  'english',
                                  coalesce(title, '') || ' ' || coalesce(text, '')
                              )
                          ) STORED
            )
            """
        ).format(
            table=table,
            dim=sql.Literal(dimensions),
            vtype=sql.SQL(self._vtype),  # validated to {vector, halfvec} — safe
        )

        # IVFFlat / HNSW need a populated table to build well; an HNSW index can
        # be created empty and is the better default for cosine KNN, so we use
        # it. ``<type>_cosine_ops`` matches the ``<=>`` operator used in search.
        create_vec_index = sql.SQL(
            "CREATE INDEX IF NOT EXISTS {name} ON {table} USING hnsw (embedding {opclass})"
        ).format(
            name=sql.Identifier(f"ix_{self._table}_embedding"),
            table=table,
            opclass=sql.SQL(f"{self._vtype}_cosine_ops"),
        )
        create_ts_index = sql.SQL(
            "CREATE INDEX IF NOT EXISTS {name} ON {table} USING gin (ts)"
        ).format(
            name=sql.Identifier(f"ix_{self._table}_ts"),
            table=table,
        )
        create_docid_index = sql.SQL(
            "CREATE INDEX IF NOT EXISTS {name} ON {table} (doc_id)"
        ).format(
            name=sql.Identifier(f"ix_{self._table}_doc_id"),
            table=table,
        )

        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            conn.commit()
            # Now that the extension surely exists, (re)register the vector
            # adapter so subsequent statements in THIS connection accept lists.
            register_vector(conn)
            with conn.cursor() as cur:
                cur.execute(create_table)
                cur.execute(create_vec_index)
                cur.execute(create_ts_index)
                cur.execute(create_docid_index)
            conn.commit()
            # CREATE TABLE IF NOT EXISTS is a no-op on a pre-existing table, so a
            # later VECTOR_TYPE change would leave the stored column at its old type
            # while we believe it is self._vtype — then every query would fail
            # opaquely ("operator does not exist: vector <=> halfvec"). Detect the
            # mismatch here and fail loudly with an actionable message instead.
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
                    "WHERE attrelid = %s::regclass AND attname = 'embedding'",
                    (self._table,),
                )
                row = cur.fetchone()
            if row:
                actual = row[0].split("(")[0].strip().lower()
                if actual != self._vtype:
                    raise ValueError(
                        f"Table {self._table!r} embedding column is {actual!r} but "
                        f"VECTOR_TYPE={self._vtype!r}. Changing vector_type requires a "
                        f"re-ingest into a fresh table: DROP TABLE {self._table}; then "
                        f"ingest again."
                    )

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------
    def upsert(
        self,
        chunks: Sequence[Chunk],
        vectors: Sequence[Sequence[float]],
    ) -> None:
        """Insert or replace ``chunks`` together with their ``vectors``.

        Upsert is keyed on the stable ``Chunk.id`` (``"{doc_id}#{ordinal}"``) so
        re-ingesting a document overwrites its prior rows rather than
        duplicating them.
        """

        rows = self._build_rows(chunks, vectors)
        if not rows:
            return
        with self._connect() as conn:
            register_vector(conn)
            with conn.cursor() as cur:
                cur.executemany(self._insert_sql(), rows)
            conn.commit()

    def replace(
        self,
        doc_id: str,
        chunks: Sequence[Chunk],
        vectors: Sequence[Sequence[float]],
        content_hash: str | None = None,
    ) -> None:
        """Atomically swap ALL of ``doc_id``'s rows (delete old → insert new) and its
        content hash in ONE transaction.

        Unlike a separate delete + upsert + set_doc_hash (three transactions), a crash
        can't leave the document half-written (rows deleted but not re-inserted, or a
        stale hash) — the whole replacement commits or none of it does.
        """

        if not doc_id:
            raise ValueError("replace requires a non-empty doc_id.")
        rows = self._build_rows(chunks, vectors)
        hashes = sql.Identifier(f"{self._table}_hashes")
        with self._connect() as conn:
            register_vector(conn)
            with conn.cursor() as cur:
                cur.execute(
                    sql.SQL("DELETE FROM {t} WHERE doc_id = %s").format(t=self._table_ident()),
                    (doc_id,),
                )
                # Only record a hash when we actually stored rows. An empty replacement
                # deleted the doc — writing a hash would mark a NON-existent doc as
                # up-to-date, so the next ingest would skip re-adding it.
                if rows:
                    cur.executemany(self._insert_sql(), rows)
                    if content_hash is not None:
                        cur.execute(
                            sql.SQL(
                                "CREATE TABLE IF NOT EXISTS {t} "
                                "(doc_id text PRIMARY KEY, content_hash text NOT NULL)"
                            ).format(t=hashes)
                        )
                        cur.execute(
                            sql.SQL(
                                "INSERT INTO {t} (doc_id, content_hash) VALUES (%s, %s) "
                                "ON CONFLICT (doc_id) DO UPDATE SET content_hash = EXCLUDED.content_hash"
                            ).format(t=hashes),
                            (doc_id, content_hash),
                        )
            conn.commit()

    def _build_rows(
        self, chunks: Sequence[Chunk], vectors: Sequence[Sequence[float]]
    ) -> list[tuple[Any, ...]]:
        """Validate + render (chunk, vector) pairs into insert rows. Shared by
        upsert/replace so the width check and column order live in one place."""

        if len(chunks) != len(vectors):
            raise ValueError(
                f"chunks/vectors length mismatch: {len(chunks)} chunks vs {len(vectors)} vectors."
            )
        rows: list[tuple[Any, ...]] = []
        for chunk, vector in zip(chunks, vectors):
            vec = list(vector)
            if self._dimensions is not None and len(vec) != self._dimensions:
                raise ValueError(
                    f"vector for chunk {chunk.id!r} has width {len(vec)} but the "
                    f"store schema expects {self._dimensions}."
                )
            rows.append(
                (
                    chunk.id,
                    chunk.doc_id,
                    chunk.title,
                    chunk.url,
                    chunk.space,
                    chunk.ordinal,
                    chunk.text,
                    self._wrap(vec),
                )
            )
        return rows

    def _insert_sql(self) -> sql.Composed:
        return sql.SQL(
            """
            INSERT INTO {table}
                (id, doc_id, title, url, space, ordinal, text, embedding)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                doc_id    = EXCLUDED.doc_id,
                title     = EXCLUDED.title,
                url       = EXCLUDED.url,
                space     = EXCLUDED.space,
                ordinal   = EXCLUDED.ordinal,
                text      = EXCLUDED.text,
                embedding = EXCLUDED.embedding
            """
        ).format(table=self._table_ident())

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------
    def search(
        self,
        query_vector: Sequence[float],
        query_text: str,
        top_k: int,
        filters: dict | None = None,
    ) -> list[ScoredChunk]:
        """Hybrid KNN + full-text search fused with Reciprocal Rank Fusion.

        Returns up to ``top_k`` :class:`ScoredChunk` ordered by fused score
        (descending). The score is the RRF score, which is positive and grows
        with how highly a row ranks in either constituent list.
        """

        if top_k <= 0:
            return []

        # The query process never calls ensure_schema, so discover the stored
        # embedding type+width from the DB once — this activates the width guard
        # below AND catches a VECTOR_TYPE/column mismatch up front (clear error)
        # instead of an opaque operator error mid-query.
        if self._dimensions is None:
            self._discover_schema()

        qvec = list(query_vector)
        if self._dimensions is not None and len(qvec) != self._dimensions:
            raise ValueError(
                f"query_vector width {len(qvec)} does not match store dimensions "
                f"{self._dimensions}."
            )

        where_sql, where_params = self._build_filter_clause(filters)

        # We over-fetch each constituent ranking (``pool``) so RRF has enough
        # candidates to fuse before truncating to ``top_k``.
        pool = max(top_k * 4, top_k)

        table = self._table_ident()
        # The vector CTE applies the metadata filter as its own ``WHERE``; the
        # lexical CTE already has a ``WHERE`` for the ts predicate, so the same
        # filter is glued on with ``AND``. Build both variants so the SQL stays
        # valid whether or not a filter is present.
        vec_where = (
            sql.SQL("WHERE {f}").format(f=where_sql) if where_sql is not None else sql.SQL("")
        )
        lex_extra = sql.SQL("AND {f}").format(f=where_sql) if where_sql is not None else sql.SQL("")

        # Two CTEs: dense KNN by cosine distance and lexical ranking by
        # websearch_to_tsquery. Each assigns a 1-based rank via ROW_NUMBER. The
        # outer query full-outer-joins them on id and sums the RRF terms.
        query = sql.SQL(
            """
            WITH params AS (
                SELECT %(qvec)s::{vtype} AS qvec,
                       websearch_to_tsquery('english', %(qtext)s) AS qts,
                       CASE WHEN %(qor)s::text IS NULL THEN NULL
                            ELSE to_tsquery('english', %(qor)s::text) END AS qor
            ),
            vec AS (
                SELECT c.id,
                       ROW_NUMBER() OVER (
                           ORDER BY c.embedding <=> p.qvec
                       ) AS rnk
                FROM {table} c, params p
                {vec_where}
                ORDER BY c.embedding <=> p.qvec
                LIMIT %(pool)s
            ),
            lex AS (
                -- Primary: websearch AND semantics. Fallback (qor, set ONLY when
                -- the AND query matched zero rows): OR over the question's terms,
                -- so one absent word can't zero out the whole lexical arm.
                SELECT c.id,
                       ROW_NUMBER() OVER (
                           ORDER BY GREATEST(
                               COALESCE(ts_rank_cd(c.ts, p.qts), 0),
                               COALESCE(ts_rank_cd(c.ts, p.qor), 0)
                           ) DESC
                       ) AS rnk
                FROM {table} c, params p
                WHERE ((p.qts IS NOT NULL AND c.ts @@ p.qts)
                       OR (p.qor IS NOT NULL AND c.ts @@ p.qor)) {lex_extra}
                ORDER BY GREATEST(
                    COALESCE(ts_rank_cd(c.ts, p.qts), 0),
                    COALESCE(ts_rank_cd(c.ts, p.qor), 0)
                ) DESC
                LIMIT %(pool)s
            ),
            fused AS (
                SELECT COALESCE(vec.id, lex.id) AS id,
                       COALESCE(1.0 / (%(rrf_k)s + vec.rnk), 0.0)
                     + COALESCE(1.0 / (%(rrf_k)s + lex.rnk), 0.0) AS score
                FROM vec
                FULL OUTER JOIN lex ON vec.id = lex.id
            )
            SELECT c.id, c.doc_id, c.title, c.url, c.space, c.ordinal, c.text,
                   f.score,
                   (1 - (c.embedding <=> p.qvec)) AS vector_score
            FROM fused f
            JOIN {table} c ON c.id = f.id
            CROSS JOIN params p
            ORDER BY f.score DESC
            LIMIT %(top_k)s
            """
        ).format(
            table=table,
            vec_where=vec_where,
            lex_extra=lex_extra,
            vtype=sql.SQL(self._vtype),  # validated to {vector, halfvec}
        )

        named_params: dict[str, Any] = {
            "qvec": self._wrap(qvec),
            "qtext": query_text or "",
            "qor": None,  # set below only when the AND query has zero matches
            "pool": pool,
            "rrf_k": _RRF_K,
            "top_k": top_k,
            **where_params,
        }

        with self._connect() as conn:
            register_vector(conn)
            # OR-fallback probe: websearch_to_tsquery ANDs every term, so a single
            # question word absent from the corpus zeroes out the whole lexical
            # arm. One cheap indexed EXISTS tells us; only then do we enable the
            # OR-of-terms fallback — every already-validated query (AND has hits)
            # behaves byte-identically.
            if query_text:
                with conn.cursor() as cur:
                    cur.execute(
                        sql.SQL(
                            "SELECT EXISTS(SELECT 1 FROM {table} WHERE ts @@ "
                            "websearch_to_tsquery('english', %s))"
                        ).format(table=table),
                        (query_text,),
                    )
                    has_and_hit = bool(cur.fetchone()[0])
                if not has_and_hit:
                    terms = re.findall(r"[a-z0-9]+", query_text.lower())
                    terms = [t for t in terms if len(t) >= 2][:12]
                    if terms:
                        named_params["qor"] = " | ".join(terms)
            with conn.cursor(row_factory=dict_row) as cur:
                # HNSW returns at most ``hnsw.ef_search`` candidates per scan; the
                # default (40) is below our over-fetch ``pool`` (up to 80), which
                # silently caps dense recall. Raise it for THIS transaction so the
                # dense arm can actually return the pool we asked for. SET LOCAL is
                # scoped to the txn, so it never leaks to other sessions.
                cur.execute(
                    sql.SQL("SET LOCAL hnsw.ef_search = {v}").format(
                        v=sql.Literal(max(pool * 2, 100))
                    )
                )
                cur.execute(query, named_params)
                fetched = cur.fetchall()

        results: list[ScoredChunk] = []
        for row in fetched:
            chunk = Chunk(
                id=row["id"],
                doc_id=row["doc_id"],
                title=row["title"],
                url=row["url"],
                text=row["text"],
                space=row["space"],
                ordinal=row["ordinal"],
            )
            results.append(
                ScoredChunk(
                    chunk=chunk,
                    score=float(row["score"]),
                    vector_score=float(row["vector_score"]),
                )
            )
        return results

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------
    def delete(self, doc_id: str) -> None:
        """Remove every chunk row belonging to ``doc_id`` (and its content hash)."""

        if not doc_id:
            raise ValueError("delete requires a non-empty doc_id.")
        stmt = sql.SQL("DELETE FROM {table} WHERE doc_id = %s").format(table=self._table_ident())
        # The hashes side table is created lazily, so guard with to_regclass —
        # one DO block, no savepoint/rollback dance when it doesn't exist yet.
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(stmt, (doc_id,))
                cur.execute(
                    sql.SQL(
                        "DO $$ BEGIN "
                        "IF to_regclass({lit}) IS NOT NULL THEN "
                        "DELETE FROM {table} WHERE doc_id = {doc}; "
                        "END IF; END $$"
                    ).format(
                        lit=sql.Literal(f"{self._table}_hashes"),
                        table=sql.Identifier(f"{self._table}_hashes"),
                        doc=sql.Literal(doc_id),
                    )
                )
            conn.commit()

    def current_dimensions(self) -> int | None:
        """The embedding width of the EXISTING table, or ``None`` if it doesn't exist.

        Lets ``reindex`` detect an embedding-model dimension change up front and refuse
        an unsafe in-place rebuild, rather than delete-then-fail per document.
        """

        self._discover_schema()
        return self._dimensions

    def clear_doc_hashes(self) -> None:
        """Drop the content-hash side table so the next ingest re-embeds EVERY document.

        The live vector rows are left untouched — this is how ``reindex`` forces a full
        re-embed in place WITHOUT a destructive table drop.
        """

        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    sql.SQL("DROP TABLE IF EXISTS {t}").format(
                        t=sql.Identifier(f"{self._table}_hashes")
                    )
                )
            conn.commit()

    # ------------------------------------------------------------------
    # Incremental ingest support — per-doc content hashes in a side table,
    # so an unchanged document costs ZERO embedding calls on re-ingest. Kept off
    # the VectorStore Protocol: the ingest pipeline feature-detects via hasattr.
    # ------------------------------------------------------------------
    def doc_hashes(self) -> dict[str, str]:
        """Return ``{doc_id: content_hash}`` for every known document."""

        stmt = sql.SQL("SELECT doc_id, content_hash FROM {table}").format(
            table=sql.Identifier(f"{self._table}_hashes")
        )
        with self._connect() as conn:
            with conn.cursor() as cur:
                try:
                    cur.execute(stmt)
                    return {r[0]: r[1] for r in cur.fetchall()}
                except psycopg.errors.UndefinedTable:
                    conn.rollback()  # first run: the hash side-table isn't created yet
                    return {}
                # Any OTHER DB error propagates: returning {} here would silently
                # force a full re-embed of the whole corpus with no signal.

    def set_doc_hash(self, doc_id: str, content_hash: str) -> None:
        """Upsert one document's content hash (creates the side table lazily)."""

        table = sql.Identifier(f"{self._table}_hashes")
        create = sql.SQL(
            "CREATE TABLE IF NOT EXISTS {table} "
            "(doc_id text PRIMARY KEY, content_hash text NOT NULL)"
        ).format(table=table)
        upsert = sql.SQL(
            "INSERT INTO {table} (doc_id, content_hash) VALUES (%s, %s) "
            "ON CONFLICT (doc_id) DO UPDATE SET content_hash = EXCLUDED.content_hash"
        ).format(table=table)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(create)
                cur.execute(upsert, (doc_id, content_hash))
            conn.commit()

    def list_doc_ids(self) -> list[str]:
        """Return the distinct ``doc_id`` values currently stored.

        Backed by the ``doc_id`` index created in :meth:`ensure_schema`. Returns
        an empty list when the table does not exist yet (nothing to reconcile).
        """

        stmt = sql.SQL("SELECT DISTINCT doc_id FROM {table}").format(table=self._table_ident())
        with self._connect() as conn:
            with conn.cursor() as cur:
                try:
                    cur.execute(stmt)
                    rows = cur.fetchall()
                except psycopg.errors.UndefinedTable:
                    conn.rollback()
                    return []
        return [str(row[0]) for row in rows]

    # ------------------------------------------------------------------
    # Filters
    # ------------------------------------------------------------------
    def _build_filter_clause(
        self, filters: dict | None
    ) -> tuple[sql.Composed | None, dict[str, Any]]:
        """Translate a ``filters`` dict into a parameterised SQL predicate.

        Only the whitelisted :data:`_FILTERABLE_COLUMNS` are allowed; an unknown
        key fails loudly rather than being silently ignored. A list/tuple value
        becomes an ``IN`` (ANY) constraint; a scalar becomes equality. Returns
        ``(None, {})`` when there is nothing to filter on.
        """

        if not filters:
            return None, {}

        clauses: list[sql.Composed] = []
        params: dict[str, Any] = {}
        for idx, (key, value) in enumerate(filters.items()):
            if key not in _FILTERABLE_COLUMNS:
                raise ValueError(
                    f"Unsupported search filter {key!r}; allowed: {sorted(_FILTERABLE_COLUMNS)}."
                )
            param_name = f"flt_{idx}"
            col = sql.Identifier(key)
            if isinstance(value, (list, tuple, set)):
                params[param_name] = list(value)
                clauses.append(
                    sql.SQL("c.{col} = ANY(%({p})s)").format(col=col, p=sql.SQL(param_name))
                )
            else:
                params[param_name] = value
                clauses.append(sql.SQL("c.{col} = %({p})s").format(col=col, p=sql.SQL(param_name)))

        predicate = sql.SQL(" AND ").join(clauses)
        return predicate, params

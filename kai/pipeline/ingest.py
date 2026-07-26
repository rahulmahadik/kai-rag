"""Ingestion pipeline: knowledge-base pages → chunks → vectors → vector store.

``ingest`` is the write side of KAI. It pulls every :class:`~kai.interfaces.Doc`
from the configured :class:`~kai.interfaces.KBSource`, chunks each one, embeds the
chunks in batches, and upserts them into the :class:`~kai.interfaces.VectorStore`.

It is idempotent: chunk ids are stable (``"{doc_id}#{ordinal}"``) so re-ingesting
the same content overwrites prior rows rather than duplicating them. The schema
is (re)ensured first, sized to the embedder's dimensionality.

Pure orchestration. It only depends on the provider Protocols and the chunker,
so it imports nothing heavy.
"""

from __future__ import annotations

import hashlib
import logging

from kai.interfaces import Chunk, Embedder, KBSource, VectorStore
from kai.pipeline.chunk import chunk_document

logger = logging.getLogger("kai.ingest")

# Embed in batches so a large space doesn't build one giant request, while still
# amortising per-call overhead. Tunable; 64 is a sensible default.
_EMBED_BATCH = 64


def ingest(
    providers: tuple,
    *,
    target_tokens: int | None = None,
    overlap_tokens: int | None = None,
    passage_prefix: str = "",
    prune: bool = False,
) -> int:
    """Ingest every page from the KB source into the vector store.

    ``providers`` is the 5-tuple returned by
    :func:`kai.factory.build_providers`, ``(embedder, llm, store, kb, tracker)``.
    Only the embedder, store and kb are used here. ``target_tokens`` /
    ``overlap_tokens`` override the chunk sizing (keep chunks within the embedding
    model's context window). ``passage_prefix`` is prepended to each chunk before
    embedding (e.g. ``"search_document: "`` for nomic-embed). ``prune`` removes
    documents that vanished upstream, only pass it for a FULL whole-space crawl.
    Returns the number of chunks upserted.
    """

    embedder, _llm, store, kb, _tracker = providers
    return ingest_from(
        embedder,
        store,
        kb,
        target_tokens=target_tokens,
        overlap_tokens=overlap_tokens,
        passage_prefix=passage_prefix,
        prune=prune,
    )


def ingest_from(
    embedder: Embedder,
    store: VectorStore,
    kb: KBSource,
    *,
    target_tokens: int | None = None,
    overlap_tokens: int | None = None,
    passage_prefix: str = "",
    prune: bool = False,
) -> int:
    """Explicit-provider variant of :func:`ingest` (typed, no tuple unpacking).

    Ensures the schema for the embedder's dimensionality, then for each document:
    chunk → embed → **replace** (delete the document's prior rows, then upsert the
    current ones). The delete-then-upsert keeps the store an exact mirror of the
    document's CURRENT chunk set, so a page that shrank no longer leaves orphaned
    higher-ordinal chunks behind (which would stay retrievable and citable).
    Embedding happens BEFORE the delete so an embedder failure can never wipe a
    document's existing rows.

    When ``prune`` is True, any ``doc_id`` in the store that was NOT seen in this
    crawl is deleted too. This reconciles pages removed/unpublished upstream.
    Only safe for a FULL whole-space crawl: a capped or subtree crawl does not see
    every live document, so pruning there would delete valid pages.

    Returns the total chunk count written.
    """

    store.ensure_schema(embedder.dimensions)

    chunk_kwargs: dict = {}
    if target_tokens:
        chunk_kwargs["target_tokens"] = target_tokens
    if overlap_tokens is not None:
        chunk_kwargs["overlap_tokens"] = overlap_tokens

    # Incremental re-ingest: per-doc content hashes let an unchanged document
    # skip chunk+embed+upsert entirely. Feature-detected so stores (and test fakes)
    # without hash support keep the previous always-reingest behavior.
    known_hashes: dict[str, str] = store.doc_hashes() if hasattr(store, "doc_hashes") else {}

    total = 0
    docs = empty = failed = pruned = skipped = 0
    seen_doc_ids: set[str] = set()
    for doc in kb.iter_pages():
        docs += 1
        seen_doc_ids.add(doc.id)  # add BEFORE work so prune never deletes a doc we
        #                           saw but failed to (re)ingest, its old rows stay.
        try:
            # Hash covers everything that shapes the stored chunks: body, title
            # (prefixed into chunks), content handling, and the chunking params.
            doc_hash = hashlib.sha256(
                "\x00".join(
                    [
                        doc.title or "",
                        getattr(doc, "content_type", "html"),
                        f"{target_tokens}:{overlap_tokens}:{passage_prefix}",
                        # url + space so a moved/re-spaced page re-ingests with the
                        # corrected citation metadata instead of being skipped.
                        getattr(doc, "url", "") or "",
                        getattr(doc, "space", "") or "",
                        doc.html or "",
                    ]
                ).encode("utf-8", "replace")
            ).hexdigest()
            if known_hashes.get(doc.id) == doc_hash:
                skipped += 1
                continue
            chunks = chunk_document(doc, **chunk_kwargs)
            if not chunks:
                # A document with no extractable text: clear any prior rows (keep the
                # store an exact mirror) and move on: not a failure, just empty.
                empty += 1
                logger.info(
                    "kai_ingest_doc_empty doc_id=%r title=%r", doc.id, (doc.title or "")[:60]
                )
                store.delete(doc.id)
                continue
            # Embed first (no store mutation yet) so a failure leaves prior rows intact.
            vectors = _embed_chunks(embedder, chunks, passage_prefix)
            # Replace this document's rows: clear the old set (incl. any orphaned
            # ordinals from a now-shorter page), then write the current chunks. Prefer
            # the ATOMIC replace (delete + insert + hash in one transaction) so a crash
            # can't leave the doc half-written; fall back for stores without it.
            if hasattr(store, "replace"):
                store.replace(doc.id, chunks, vectors, content_hash=doc_hash)
            else:
                store.delete(doc.id)
                store.upsert(chunks, vectors)
                if hasattr(store, "set_doc_hash"):
                    store.set_doc_hash(doc.id, doc_hash)
            total += len(chunks)
        except Exception as exc:  # noqa: BLE001 - one bad doc must not abort the crawl
            failed += 1
            logger.warning(
                "kai_ingest_doc_failed doc_id=%r title=%r err=%s: %s",
                doc.id,
                (doc.title or "")[:60],
                type(exc).__name__,
                exc,
            )
            continue

    if prune and getattr(kb, "errors", 0):
        logger.warning(
            "kai_prune_skipped source_errors=%d: a source failed mid-crawl, so its "
            "un-crawled docs would look missing; not pruning this run.",
            kb.errors,
        )
    elif prune:
        existing = store.list_doc_ids()
        # NEVER prune curated answers (the Inform loop): they have no crawl source,
        # so a KBSource crawl never "sees" them, without this exclusion they'd
        # always look missing and get deleted, destroying human-reviewed answers.
        from kai.pipeline.inform import CURATED_SPACE

        # Keep = docs we YIELDED plus docs the source SAW-but-skipped (empty body,
        # permission blip, too-large). Only docs the source never saw at all (i.e.
        # truly deleted upstream) are candidates for prune.
        keep = seen_doc_ids | (getattr(kb, "seen_ids", None) or set())
        missing = [d for d in existing if d not in keep and not d.startswith(f"{CURATED_SPACE}:")]
        # Mass-delete guard: a "full" crawl that silently came back partial (auth
        # change, upstream outage, pagination bug) must not wipe the corpus. If
        # pruning would remove more than half of the existing docs, refuse and log
        # loudly. An operator can drop the table deliberately if that's intended.
        if docs == 0 or (existing and len(missing) > len(existing) // 2):
            logger.error(
                "kai_prune_refused crawled=%d existing=%d would_delete=%d, crawl "
                "looks partial; refusing to mass-delete (drop the table to reset).",
                docs,
                len(existing),
                len(missing),
            )
        else:
            for doc_id in missing:
                store.delete(doc_id)
                pruned += 1

    # One structured summary line per ingest: the operator's at-a-glance health
    # check (failures are surfaced, never silent; a systematic outage shows as a
    # high failed= with chunks=0).
    logger.info(
        "kai_ingest_complete docs=%d chunks=%d empty=%d failed=%d pruned=%d skipped_unchanged=%d",
        docs,
        total,
        empty,
        failed,
        pruned,
        skipped,
    )
    return total


def reindex(
    providers: tuple,
    *,
    target_tokens: int | None = None,
    overlap_tokens: int | None = None,
    passage_prefix: str = "",
    prune: bool = True,
    inform_store=None,  # noqa: ANN001 - InformStore | None; kept loose to avoid a cycle
) -> dict:
    """Rebuild the vector index, data-safely, PRESERVING curated/inform/telemetry.

    The targeted alternative to ``reset-db`` (which drops the whole database). Unlike a
    drop-then-reingest, this can NEVER wipe the corpus on a failed/empty crawl:

    1. clear the content hashes so every document re-embeds (the live rows stay);
    2. re-ingest every source (each doc replaced per-doc, embed BEFORE delete). When
       ``prune`` is set, docs gone upstream are removed too, but ONLY pass it for a
       FULL crawl; a capped/subtree crawl can't see every page, so pruning it would
       delete valid docs. The mass-delete / ``kb.errors`` guards are a backstop only;
    3. re-index every APPROVED curated answer from the Inform queue.

    An embedding-DIMENSION change can't be applied in place (delete-then-upsert would
    fail per doc and lose rows). It raises, directing the operator to ``reset-db``.

    Returns ``{"chunks": n, "curated": m}``.
    """

    embedder, _llm, store, _kb, _tracker = providers
    if not hasattr(store, "clear_doc_hashes"):
        raise RuntimeError("This vector store doesn't support reindex.")
    # Data-safe rebuild: re-embed every doc IN PLACE (per-doc embed-before-delete +
    # the prune mass-delete/kb.errors guards), so a failed/empty crawl can NEVER wipe
    # the corpus and readers keep seeing the old index until each doc is replaced.
    # A change in embedding DIMENSION can't be done in place (delete-then-upsert would
    # fail per doc and lose rows), refuse it loudly rather than destroy data.
    current = store.current_dimensions() if hasattr(store, "current_dimensions") else None
    if current is not None and current != embedder.dimensions:
        raise RuntimeError(
            f"Embedding dimension changed ({current} -> {embedder.dimensions}); reindex "
            "can't rebuild in place without risking data loss. Back up, then run "
            "`reset-db` and `ingest` to rebuild from a fresh table."
        )
    logger.info("kai_reindex_start, re-embedding all sources in place")
    store.ensure_schema(embedder.dimensions)
    store.clear_doc_hashes()  # force a full re-embed (every doc now hash-misses)
    chunks = ingest(
        providers,
        target_tokens=target_tokens,
        overlap_tokens=overlap_tokens,
        passage_prefix=passage_prefix,
        # Only prune on a FULL crawl. A capped (CONFLUENCE_MAX_DOCS) or subtree
        # (CONFLUENCE_ROOT_PAGE) crawl never sees every live page, so pruning would
        # delete valid docs beyond the cap: the caller decides via ``prune``.
        prune=prune,
    )
    curated = _reindex_curated(
        embedder,
        store,
        inform_store,
        target_tokens=target_tokens,
        overlap_tokens=overlap_tokens,
        passage_prefix=passage_prefix,
    )
    logger.info("kai_reindex_complete chunks=%d curated=%d", chunks, curated)
    return {"chunks": chunks, "curated": curated}


def _reindex_curated(
    embedder: Embedder,
    store: VectorStore,
    inform_store,  # noqa: ANN001 - InformStore | None
    *,
    target_tokens: int | None,
    overlap_tokens: int | None,
    passage_prefix: str,
) -> int:
    """Re-index every approved curated answer after a vector wipe. Returns the count.

    The curated chunks were dropped with the table; their source Q&A lives in the
    Inform queue, so we re-synthesize each approved candidate back into the index.
    """

    if inform_store is None:
        return 0
    from kai.pipeline.inform import index_curated_answer

    done, offset, page = 0, 0, 500
    while True:  # page through ALL approved candidates: no silent 500-row cap
        batch = inform_store.list(status="approved", limit=page, offset=offset)
        if not batch:
            break
        for cand in batch:
            try:
                n = index_curated_answer(
                    cand["question"],
                    cand["answer"],
                    embedder,
                    store,
                    candidate_id=cand["id"],
                    target_tokens=target_tokens or 500,
                    overlap_tokens=overlap_tokens if overlap_tokens is not None else 60,
                    passage_prefix=passage_prefix,
                )
                if n:
                    done += 1
            except Exception as exc:  # noqa: BLE001 - one bad candidate must not abort reindex
                logger.warning(
                    "kai_reindex_curated_failed id=%s err=%s", cand.get("id"), type(exc).__name__
                )
        offset += len(batch)
        if len(batch) < page:
            break
    return done


def _embed_chunks(
    embedder: Embedder,
    chunks: list[Chunk],
    passage_prefix: str = "",
) -> list[list[float]]:
    """Embed every chunk in ``_EMBED_BATCH``-sized batches; return all vectors.

    Each chunk's text is prefixed with ``passage_prefix`` (model task instruction,
    e.g. ``"search_document: "``) before embedding. Returns vectors aligned 1:1
    with ``chunks`` so the caller can upsert the whole document atomically.
    """

    vectors: list[list[float]] = []
    for start in range(0, len(chunks), _EMBED_BATCH):
        batch = chunks[start : start + _EMBED_BATCH]
        vecs = embedder.embed([f"{passage_prefix}{c.text}" for c in batch])
        if len(vecs) != len(batch):
            raise RuntimeError(
                f"embedder returned {len(vecs)} vectors for {len(batch)} "
                "chunks; refusing to upsert a misaligned batch."
            )
        vectors.extend(vecs)
    return vectors

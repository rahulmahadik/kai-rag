#!/usr/bin/env python3
"""Generic per-space validation, point KAI at ANY Confluence space and prove the
same code (a) retrieves that corpus, (b) answers its questions, (c) escalates
everything it can't support (other-domain, out-of-scope, fabrication).

Three checks, increasing cost:
  1. RECALL (deterministic, no answer LLM): probe every page by its title; is the
     page's own content retrieved and above the confidence gate?
  2. IN-SCOPE ANSWER (LLM): the content-richest pages, asked plainly, must ANSWER.
  3. SCOPING (LLM): cross-domain (Kafka) + generic OOS + a fabrication probe must
     ESCALATE, proving nothing is hard-coded to one corpus.

    .venv/bin/python eval/validate_space.py --space ZOOKEEPER --table kai_zk --max-docs 50 --ingest
    .venv/bin/python eval/validate_space.py --space ZOOKEEPER --table kai_zk            # validate

In-process (no HTTP server), so it never touches the running Kafka API.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kai.config import Settings  # noqa: E402
from kai.factory import build_providers  # noqa: E402
from kai.pipeline.ask import _confidence, _rerank, answer_question  # noqa: E402
from kai.pipeline.ingest import ingest_from  # noqa: E402

# Other-domain (Kafka) + generic OOS + a fabrication probe. On ANY non-Kafka
# space these must ESCALATE. The SSL probe asks for exact config that no general
# wiki will contain verbatim: the fabrication guard must catch it.
SCOPING_PROBES = [
    ("cross-domain", "What is the purpose of replication in Kafka?"),
    ("cross-domain", "How does Kafka partition leader election work?"),
    ("oos", "What is the capital of France?"),
    ("oos", "What is the company VPN password reset procedure?"),
    ("oos", "What is the recipe for chocolate chip cookies?"),
    (
        "fabrication",
        "What exact broker config settings enable SSL encryption between Kafka brokers?",
    ),
    ("fabrication", "How much does a commercial enterprise license cost per year in US dollars?"),
]

_SKIP_TITLES = {".bookmarks", "Example Emails", "Whiteboard", "Index", "Powered By"}


def _settings(space: str, table: str, max_docs: int) -> Settings:
    return Settings(confluence_space_key=space, vector_table=table, confluence_max_docs=max_docs)


def _rows(store, query: str):
    import psycopg
    from psycopg import sql

    with psycopg.connect(store._database_url) as conn, conn.cursor() as cur:
        cur.execute(sql.SQL(query).format(t=sql.Identifier(store._table)))
        return cur.fetchall()


def _retrieve_det(emb, store, s, query: str):
    """Deterministic single-query retrieve + rerank (no multi-query)."""
    qp = getattr(s, "embed_query_prefix", "") or ""
    qvec = emb.embed([f"{qp}{query}"])[0]
    pool = s.rerank_candidates if (s.reranker or "noop").lower() != "noop" else s.top_k
    scored = store.search(query_vector=qvec, query_text=query, top_k=pool)
    return _rerank(scored, s.reranker, query=query, model=s.reranker_model, top_k=s.top_k)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--space", required=True)
    ap.add_argument("--table", required=True)
    ap.add_argument("--max-docs", type=int, default=0)
    ap.add_argument("--ingest", action="store_true")
    ap.add_argument("--answers", type=int, default=5, help="how many in-scope answer checks")
    args = ap.parse_args()

    s = _settings(args.space, args.table, args.max_docs)
    emb, llm, store, kb, tracker = build_providers(s)

    if args.ingest:
        n = ingest_from(
            emb,
            store,
            kb,
            target_tokens=s.chunk_target_tokens,
            overlap_tokens=s.chunk_overlap_tokens,
            passage_prefix=s.embed_passage_prefix,
            prune=False,
        )
        print(f"ingested {n} chunks from {args.space} -> {args.table}")
        return

    titles = [r[0] for r in _rows(store, "SELECT DISTINCT title FROM {t} ORDER BY 1")]
    by_len = [
        r[0]
        for r in _rows(
            store, "SELECT title, sum(length(text)) FROM {t} GROUP BY title ORDER BY 2 DESC"
        )
    ]
    content = [t for t in by_len if t not in _SKIP_TITLES]

    threshold = s.confidence_threshold
    print(
        f"\n=== validate space={args.space} table={args.table} · {len(titles)} pages · threshold={threshold} ===\n"
    )

    # 1. RECALL sweep (deterministic)
    findable = 0
    misses = []
    for t in titles:
        scored = _retrieve_det(emb, store, s, t)
        ids_titles = [sc.chunk.title for sc in scored]
        conf = _confidence(t, scored)
        ok = (t in ids_titles) and conf >= threshold
        findable += ok
        if not ok:
            misses.append((t, round(conf, 3)))
    print(
        f"[1] RECALL: {findable}/{len(titles)} pages findable by their own title (conf>= {threshold})"
    )
    if misses:
        print(f"    not findable: {misses[:8]}")

    # 2. IN-SCOPE answers (must ANSWER)
    print("\n[2] IN-SCOPE answers (content-richest pages, must ANSWER):")
    ans_ok = 0
    for t in content[: args.answers]:
        a = answer_question(f"What is {t}?", emb, llm, store, tracker, s)
        ans_ok += not a.escalated
        print(
            f"    [{'ok ' if not a.escalated else 'MISS'}] esc={a.escalated} conf={a.confidence:.3f}  '{t[:40]}'"
        )

    # 3. SCOPING (must ESCALATE)
    print("\n[3] SCOPING (cross-domain + OOS + fabrication, must ESCALATE):")
    scope_ok = 0
    leaks = []
    for kind, q in SCOPING_PROBES:
        a = answer_question(q, emb, llm, store, tracker, s)
        good = a.escalated
        scope_ok += good
        if not good:
            leaks.append((kind, q, a.answer[:80]))
        print(
            f"    [{'ok ' if good else 'LEAK'}] {kind:12s} esc={a.escalated} conf={a.confidence:.3f}  {q[:48]}"
        )
    for kind, q, ans in leaks:
        print(f"      !! LEAK {kind}: {q}\n         answered: {ans}")

    good = findable + ans_ok + scope_ok
    wrong = len(leaks)
    print(
        f"\n=== {args.space}: recall {findable}/{len(titles)} · answers {ans_ok}/{min(args.answers, len(content))} · "
        f"scoping {scope_ok}/{len(SCOPING_PROBES)} · WRONG(leaks)={wrong} ==="
    )


if __name__ == "__main__":
    main()

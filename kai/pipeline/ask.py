"""Question-answering pipeline: retrieve → answer → cite → maybe escalate.

``ask`` is the read side of KAI and ties every provider together:

1. embed the question with the :class:`~kai.interfaces.Embedder`;
2. retrieve the top-k :class:`~kai.interfaces.ScoredChunk` from the
   :class:`~kai.interfaces.VectorStore` (hybrid vector + lexical fusion);
3. (optionally) rerank the candidate pool with a cross-encoder, then keep top-k;
4. build a grounded, citation-instructed prompt and call the
   :class:`~kai.interfaces.LLMClient`;
5. compute a confidence score from retrieval strength;
6. if confidence is below ``settings.confidence_threshold`` OR no chunks were
   retrieved OR the model could not answer, open a tracker issue and return an
   **escalated** :class:`~kai.interfaces.Answer`; otherwise return a confident,
   cited answer.

Citations are derived from the chunks actually shown to the model, de-duplicated
by URL. Pure orchestration over the provider Protocols.
"""

from __future__ import annotations

import logging
import math
import re
from functools import lru_cache

from kai.config import Settings
from kai.interfaces import (
    Answer,
    Chunk,
    Citation,
    Embedder,
    LLMClient,
    ScoredChunk,
    Tracker,
    VectorStore,
)
from kai.pipeline.chunk import chunk_body
from kai.pipeline.prompt import IDK_MARKER, build_prompt

logger = logging.getLogger("kai.ask")

# Inline /ask-document cap: beyond this many chunks a file should be ingested into
# the corpus rather than embedded ad-hoc (bounds memory + embed round-trips).
_MAX_DOC_CHUNKS = 400


def _embed_in_batches(embedder, texts: list[str], batch: int = 64) -> list[list[float]]:
    """Embed ``texts`` in batches so a large document never posts one giant request
    (and so an embedder 4xx falls back per-batch, not per-whole-document)."""

    out: list[list[float]] = []
    for start in range(0, len(texts), batch):
        out.extend(embedder.embed(texts[start : start + batch]))
    return out


# A refusal STARTS with one of these (the prompt instructs the model to reply
# exactly "I don't know"). Matching at the start stops a good cited answer that
# merely mentions e.g. "the page does not contain pricing" mid-sentence from
# being misread as a refusal.
_IDK_PREFIXES = (
    IDK_MARKER.lower(),  # "i don't know"
    "i do not know",
    "i cannot answer",
    "i can't answer",
    "i am unable to answer",
    "i'm unable to answer",
)
# Refusal phrases that, in an UNCITED reply, mark an "I don't know" hedge. A
# genuine answer cites its source ([n]); a hedge does not, so these only fire
# when no citation marker is present.
#
# STRONG phrases are self-referential refusals that essentially never occur inside
# substantive knowledge-base prose, so they fire on their own.
_IDK_STRONG = (
    "i do not know",
    "i don't know",
    "do not have enough",
    "don't have enough",
    "not enough information",
    "no information about",
    "i am unable to answer",
    "i'm unable to answer",
    "i am not able to answer",
    "i'm not able to answer",
)
# WEAK phrases ALSO occur naturally in real content ("the controller could not
# find a leader", "a broker cannot answer a fetch in time"), so they only count as
# a refusal when paired with a self-referential ANCHOR pointing at the sources /
# context / question: the shape of an actual "the sources don't cover this" hedge.
_IDK_WEAK = (
    "does not contain",
    "doesn't contain",
    "could not find",
    "couldn't find",
    "cannot answer",
    "can't answer",
    "unable to answer",
    "not able to answer",
    "no mention of",
    "is not mentioned",
)
# Anchors that mark a weak phrase as being ABOUT the sources, not about the domain.
_IDK_ANCHORS = (
    "the source",
    "the sources",
    "the context",
    "the provided",
    "provided context",
    "knowledge base",
    "the document",
    "the documents",
    "the passage",
    "the passages",
    "available information",
    "information about",
    "any information",
    "enough information",
    "the information provided",
    "this question",
    "the question",
)
# Leading hedges to strip before the opener check ("Unfortunately, I cannot...").
_IDK_HEDGES = (
    "unfortunately,",
    "unfortunately ",
    "i'm sorry,",
    "i am sorry,",
    "sorry,",
    "sorry ",
    "hmm,",
    "hmm ",
    "well,",
)

# Content-word overlap (stopwords excluded) grounds confidence in topical
# relevance. The store's fused score is rank-based, so on a small corpus even an
# off-topic query scores near the top; overlap distinguishes the two.
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS: frozenset[str] = frozenset(
    """
    a an and any are as at be by can do does for from how i in into is it its
    me my no not of on or our the their there this to up us was what when where
    which who whom why will with you your com s t re ll ve
    """.split()  # noqa: SIM905 - a wrapped word list reads better than 50 quoted items
)


# Specifics a fabricated answer tends to invent: qualified dotted identifiers and
# config-style URIs. 3+ segments so ordinary idioms like ``obj.method`` are not
# flagged; http(s) links are ignored (harmless reformatting, low risk).
_SPECIFIC_DOTTED_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*){2,}\b")
_SPECIFIC_URI_RE = re.compile(r"(?:[A-Za-z][A-Za-z0-9+.\-]*://|jdbc:)[^\s)\]}>\"']+")
# Dotted strings that are ordinary prose/abbreviations, not technical specifics.
_SPECIFIC_IGNORE: frozenset[str] = frozenset(
    {"e.g", "i.e", "etc", "a.k.a", "p.m", "a.m", "u.s", "u.s.a"}
)


def _cosine(a, b) -> float:  # noqa: ANN001 - float sequences
    """Cosine similarity of two equal-length float vectors."""

    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


@lru_cache(maxsize=4096)
def _content_tokens(text: str) -> frozenset[str]:
    """Meaningful (non-stopword, length>=2) lowercase tokens of ``text``.

    Memoized (and returns an immutable frozenset): the same chunk text is tokenised
    by the confidence gate AND both grounding guards within one request, and again
    across requests, so caching removes the repeated regex work. Callers union it
    into a fresh set, never mutate the result, so sharing the cached value is safe.
    """

    return frozenset(
        tok for tok in _TOKEN_RE.findall(text.lower()) if len(tok) >= 2 and tok not in _STOPWORDS
    )


# Lead-ins carry no information but wreck the cross-encoder's score: "show me
# details of RFC 1918" scores 0.38, bare "RFC 1918" scores 5.3, same page. Stripped
# for retrieval and rerank only; the original question still drives the prompt and
# the ticket. The topic is unchanged, so this cannot make an out-of-scope question
# answerable.
_QUERY_LEADINS = (
    "can you please tell me about",
    "can you please tell me",
    "can you tell me about",
    "can you tell me",
    "can you show me",
    "can you give me",
    "can you explain",
    "could you please tell me",
    "could you tell me about",
    "could you tell me",
    "could you show me",
    "could you give me",
    "could you explain",
    "would you tell me",
    "please tell me about",
    "please tell me",
    "please show me the",
    "please show me",
    "please give me",
    "please explain the",
    "please explain",
    "please describe the",
    "please describe",
    "please summarize",
    "please summarise",
    "please list",
    "i want to know about",
    "i want to know",
    "i would like to know about",
    "i would like to know",
    "i'd like to know about",
    "i'd like to know",
    "i need to know about",
    "i need to know",
    "i wanna know about",
    "tell me about",
    "tell me",
    "show me the details of",
    "show me details of",
    "show me details about",
    "show me all the",
    "show me the",
    "show me",
    "give me the details of",
    "give me details of",
    "give me details about",
    "give me the",
    "give me",
    "walk me through the",
    "walk me through",
    "explain the",
    "explain",
    "describe the",
    "describe",
    "summarize the",
    "summarize",
    "summarise the",
    "summarise",
    "what is the status of this",
    "what is the status of",
    "what's the status of this",
    "what's the status of",
    "status of this",
    "status of",
    "more details on",
    "more details about",
    "the details of",
    "details of the",
    "details of",
    "details about",
    "details on",
    "info on",
    "info about",
    "information on",
    "information about",
    "an overview of",
    "overview of the",
    "overview of",
    "a summary of",
    "summary of",
    "please",
    "kindly",
)
# Trailing politeness / fillers to drop from the END.
_QUERY_TRAILERS = ("please", "thanks", "thank you", "thx", "pls")


def _normalize_query(question: str) -> str:
    """Strip conversational/imperative lead-ins (and trailing politeness) so the
    cross-encoder scores the actual TOPIC, not the filler.

    Iterative front-stripping (so "show me details of X" → "X"); returns the
    original whenever stripping would leave nothing substantive to retrieve on.
    """

    q = question.strip()
    # trailing politeness
    changed = True
    while changed:
        changed = False
        low = q.lower().rstrip(" .!?,")
        for tail in _QUERY_TRAILERS:
            if low.endswith(tail) and (len(low) == len(tail) or not low[-len(tail) - 1].isalnum()):
                cand = q[: len(q) - (len(q) - len(low)) - len(tail)].rstrip(" ,.!?")
                if len(_content_tokens(cand)) >= 1:
                    q, changed = cand, True
                    break
    # leading conversational filler
    changed = True
    while changed:
        changed = False
        low = q.lower()
        for lead in _QUERY_LEADINS:
            if low.startswith(lead) and (len(low) == len(lead) or not low[len(lead)].isalnum()):
                cand = q[len(lead) :].lstrip(" :,-")
                if len(_content_tokens(cand)) >= 1:  # keep something to search on
                    q, changed = cand, True
                    break
    return q.strip() or question.strip()


def ask(question: str, providers: tuple, settings: Settings) -> Answer:
    """Answer ``question`` end to end, escalating when KAI is not confident.

    ``providers`` is the 5-tuple from :func:`kai.factory.build_providers`
    - ``(embedder, llm, store, kb, tracker)``.
    """

    embedder, llm, store, _kb, tracker = providers
    return answer_question(question, embedder, llm, store, tracker, settings)


def answer_question(
    question: str,
    embedder: Embedder,
    llm: LLMClient,
    store: VectorStore,
    tracker: Tracker,
    settings: Settings,
) -> Answer:
    """Explicit-provider variant of :func:`ask` (typed, no tuple unpacking)."""

    question = (question or "").strip()
    if not question:
        raise ValueError("ask requires a non-empty question.")

    # 1-3. Retrieve: (optionally) rewrite + multi-query expand, retrieve a candidate
    #      pool per query, union, rerank against the original question, keep top_k.
    #      Shared with /search so the eval path matches production exactly.
    question, scored = retrieve(question, embedder, llm, store, settings)
    return _answer_from_retrieval(question, scored, embedder, llm, tracker, settings)


def _answer_from_retrieval(
    question: str,
    scored: list[ScoredChunk],
    embedder: Embedder,
    llm: LLMClient,
    tracker: Tracker,
    settings: Settings,
) -> Answer:
    """Produce a grounded :class:`Answer` from already-retrieved chunks.

    Shared by :func:`answer_question` (corpus retrieval) and the ad-hoc document
    Q&A path, so an answer about a dropped PDF passes the same gate and guards.
    """

    # Confidence comes from retrieval, computed before the LLM call, so the
    # escalate decision rests on evidence rather than on the model's prose.
    confidence = _confidence(
        question,
        scored,
        rerank_is_prob=getattr(settings, "rerank_score_is_probability", False),
    )

    def escalate(draft: str) -> Answer:
        """Escalate with this request's context. One call site per guard below."""

        return _escalate(
            question,
            scored,
            draft,
            confidence,
            tracker,
            include_draft=getattr(settings, "escalation_include_draft", False),
        )

    # Retrieval already failed the gate: escalate without spending a generation.
    if not scored or confidence < settings.confidence_threshold:
        return escalate("")

    # Cleared. The model is the second guard: if it cannot answer from the
    # context it replies IDK and we escalate anyway.
    system, user = build_prompt(question, scored)
    # temperature=0 keeps answers deterministic, so a question's answer/escalate
    # decision is stable run to run.
    raw_answer = llm.complete(
        system,
        user,
        max_tokens=getattr(settings, "answer_max_tokens", 1024),
        temperature=0.0,
    ).strip()
    # Drop editorialising openers the prompt forbids, and tidy whitespace.
    raw_answer = _tidy_answer(raw_answer)
    if not raw_answer or _looks_like_idk(raw_answer):
        return escalate(raw_answer)

    # The model sometimes appends an IDK after giving real information.
    raw_answer = _strip_trailing_idk(raw_answer)

    # Deterministic grounding guards, always on, no LLM call.
    # (a) concrete specifics (qualified names, config URIs) absent from every source.
    if _fabricated_specifics(raw_answer, scored):
        return escalate(raw_answer)
    # (b) a significant number absent from every source: invented totals and counts.
    bad_number = _fabricated_numbers(raw_answer, scored)
    if bad_number is not None:
        logger.info("kai_fabricated_number_escalate value=%r", bad_number)
        return escalate(raw_answer)
    # (c) too little of the answer's vocabulary drawn from the sources at all.
    grounding_min = getattr(settings, "answer_grounding_min", 0.0) or 0.0
    if grounding_min > 0 and len(_content_tokens(raw_answer)) >= 12:
        src_tokens: set[str] = set()
        for sc in scored:
            src_tokens |= _content_tokens(f"{sc.chunk.title} {sc.chunk.text}")
        # Thin sources can't cover a faithful answer's vocabulary, so low overlap
        # there is not a fabrication signal. Guards (a), IDK and verify still apply.
        if (
            len(src_tokens) >= _GROUNDING_MIN_SOURCE_TOKENS
            and _answer_grounding(raw_answer, scored) < grounding_min
        ):
            return escalate(raw_answer)

    # Sentence-level grounding (off by default): every substantive prose sentence
    # must be semantically supported by a retrieved chunk.
    if getattr(settings, "sentence_grounding", False):
        bad = _unsupported_sentences(
            raw_answer,
            scored,
            embedder,
            floor=getattr(settings, "sentence_grounding_min", 0.55),
            query_prefix=getattr(settings, "embed_query_prefix", "") or "",
            passage_prefix=getattr(settings, "embed_passage_prefix", "") or "",
        )
        if bad:
            logger.info(
                "kai_sentence_grounding_escalate unsupported=%d first=%r",
                len(bad),
                bad[0][:80],
            )
            return escalate(raw_answer)

    # Second pass: confirm the answer is supported by its sources and about the
    # right subject.
    if getattr(settings, "verify_answers", False):
        from kai.pipeline.verify import verify_answer

        if not verify_answer(llm, question, raw_answer, scored):
            return escalate(raw_answer)

    # Renumber [n] markers to match the de-duplicated Sources list 1:1. A long page
    # is many chunks, so [1]..[8] can all resolve to one source.
    raw_answer, citations = _finalize_citations(raw_answer, scored)
    # Dropping markers can leave a " ." gap, so tidy once more.
    raw_answer = _tidy_answer(raw_answer)
    # Never ship an empty confident answer.
    if not raw_answer.strip():
        return escalate(raw_answer)
    # Label curated (Inform loop) answers: community-reviewed, not official docs.
    if any(getattr(sc.chunk, "space", "") == "kai-curated" for sc in scored):
        raw_answer = raw_answer.rstrip() + (
            "\n\n_Note: this is a community-curated answer (reviewed & approved), "
            "not from official documentation._"
        )
    return Answer(
        answer=raw_answer,
        citations=citations,
        confidence=confidence,
        escalated=False,
        escalation_url=None,
    )


class _NoTracker:
    """Tracker that files nothing: ad-hoc document Q&A opens no Jira ticket."""

    def create_issue(self, title: str, body: str) -> str:  # noqa: ARG002
        return ""


def answer_from_document(
    question: str,
    text: str,
    filename: str,
    embedder: Embedder,
    llm: LLMClient,
    settings: Settings,
    *,
    content_type: str = "text",
) -> Answer:
    """Answer ``question`` grounded ONLY in an uploaded ``text`` (ad-hoc RAG).

    Chunk → embed → in-memory cosine top-k over just this document, then the SAME
    gate + grounding/verify guards as the corpus path (via _answer_from_retrieval),
    so a dropped-PDF answer can't fabricate either. No DB writes, no Jira ticket;
    when nothing in the doc supports the question it says so.

    ``content_type`` ("html"/"markdown"/"text") routes chunking exactly like the
    ingest path, so an uploaded ``.html`` is tag-stripped (not fed to the LLM as raw
    markup) and markdown gets heading-aware chunking.
    """

    question = (question or "").strip()
    if not question:
        raise ValueError("answer_from_document requires a non-empty question.")
    pieces = chunk_body(
        text or "",
        content_type=content_type,
        target_tokens=getattr(settings, "chunk_target_tokens", 500),
        overlap_tokens=getattr(settings, "chunk_overlap_tokens", 60),
    )
    if not pieces:
        return Answer(
            answer=f"**{filename}** has no readable text to answer from.",
            citations=[],
            confidence=0.0,
            escalated=True,
        )
    # Cap inline Q&A: a huge file would embed thousands of chunks in one request
    # (peak memory) and, on any embedder 4xx, fan out to one request per chunk.
    # Past the cap, point the user at ingestion instead.
    if len(pieces) > _MAX_DOC_CHUNKS:
        return Answer(
            answer=(
                f"**{filename}** is too large for inline Q&A ({len(pieces)} "
                f"chunks > {_MAX_DOC_CHUNKS}). Ingest it into the knowledge base "
                "instead, then ask normally."
            ),
            citations=[],
            confidence=0.0,
            escalated=True,
        )

    qp = getattr(settings, "embed_query_prefix", "") or ""
    pp = getattr(settings, "embed_passage_prefix", "") or ""
    # Batch the passage embeds (a single combined call fans out to one request per
    # chunk on any 4xx); embed the query separately.
    chunk_vecs = _embed_in_batches(embedder, [f"{pp}{p}" for p in pieces])
    qvec = embedder.embed([f"{qp}{question}"])[0]

    scored = []
    for i, (p, cv) in enumerate(zip(pieces, chunk_vecs, strict=True)):
        sc = _cosine(qvec, cv)  # compute once (previously evaluated twice per chunk)
        scored.append(
            ScoredChunk(
                chunk=Chunk(
                    id=f"doc#{i}",
                    doc_id=filename,
                    title=filename,
                    url="",
                    space="upload",
                    ordinal=i,
                    text=p,
                ),
                score=sc,
                vector_score=sc,
            )
        )
    scored.sort(key=lambda s: s.score, reverse=True)
    scored = scored[: getattr(settings, "top_k", 8)]

    ans = _answer_from_retrieval(question, scored, embedder, llm, _NoTracker(), settings)
    if ans.escalated:
        # Reframe the corpus "raised a ticket" wording for a document question.
        return Answer(
            answer=f"I couldn't find an answer to that in **{filename}**.",
            citations=[],
            confidence=ans.confidence,
            escalated=True,
        )
    return ans


# ---------------------------------------------------------------------------
# Retrieval (shared by /ask and /search)
# ---------------------------------------------------------------------------


def retrieve(
    question: str,
    embedder: Embedder,
    llm: LLMClient,
    store: VectorStore,
    settings: Settings,
) -> tuple[str, list[ScoredChunk]]:
    """Run the full retrieval stage and return ``(effective_question, scored)``.

    Steps: optional query rewrite → optional multi-query expansion → embed each
    query (with the model's query prefix) → hybrid search per query → union
    (dedup by chunk id, original question first) → rerank against the original
    question → top_k. ``effective_question`` is the (possibly rewritten) question
    the caller should use for the prompt and confidence so both stay consistent.

    Factored out of :func:`answer_question` so ``/search`` retrieves IDENTICALLY to
    ``/ask`` (same multi-query + rerank), instead of a weaker single-query path.
    """

    question = (question or "").strip()

    # 0. Optionally normalise messy input (fix spelling/grammar, meaning kept).
    if getattr(settings, "query_rewrite", False):
        from kai.pipeline.rewrite import rewrite_query

        question = rewrite_query(llm, question)

    # Strip conversational/imperative filler for RETRIEVAL + RERANK only (the
    # cross-encoder ranks "show me details of X" far below bare "X"). The user's
    # original question is still returned for the prompt + confidence + ticket.
    retrieval_q = (
        _normalize_query(question) if getattr(settings, "normalize_query", True) else question
    )

    rerank_on = (settings.reranker or "noop").strip().lower() != "noop"
    n_retrieve = settings.rerank_candidates if rerank_on else settings.top_k

    queries = [retrieval_q]
    if getattr(settings, "multi_query", False):
        from kai.pipeline.multiquery import expand_query

        queries += expand_query(llm, retrieval_q, n=settings.multi_query_count)

    # nomic-embed (and similar) require a query task prefix; blank for other models.
    q_prefix = getattr(settings, "embed_query_prefix", "") or ""

    # Embed all queries in ONE batched call (prefix the embedding input only;
    # lexical search gets the bare query text), then retrieve per query.
    qvecs = embedder.embed([f"{q_prefix}{q}" for q in queries])

    seen_ids: set[str] = set()
    candidates: list[ScoredChunk] = []
    for q, qvec in zip(queries, qvecs, strict=True):
        for sc in store.search(query_vector=qvec, query_text=q, top_k=n_retrieve):
            if sc.chunk.id not in seen_ids:
                seen_ids.add(sc.chunk.id)
                candidates.append(sc)

    # Rerank against the NORMALIZED query (filler stripped) so the cross-encoder
    # scores the topic; return the ORIGINAL question for the prompt/confidence.
    scored = _rerank(
        candidates,
        settings.reranker,
        query=retrieval_q,
        model=settings.reranker_model,
        top_k=settings.top_k,
    )
    return question, scored


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _rerank(
    scored: list[ScoredChunk],
    reranker: str,
    *,
    query: str = "",
    model: str = "",
    top_k: int | None = None,
) -> list[ScoredChunk]:
    """Apply the configured reranker and truncate to ``top_k``.

    ``noop`` returns the input order; ``cross_encoder`` reorders the candidates
    with a cross-encoder (precise second-stage relevance).
    """

    name = (reranker or "noop").strip().lower()
    if name == "noop":
        return scored[:top_k] if top_k else scored
    if name in ("cross_encoder", "cross-encoder", "bge"):
        from kai.providers.reranker import rerank as _ce_rerank

        return _ce_rerank(query, scored, model, top_k=top_k)
    raise ValueError(f"Unsupported reranker {reranker!r}; use 'noop' or 'cross_encoder'.")


def _confidence(question: str, scored: list[ScoredChunk], rerank_is_prob: bool = False) -> float:
    """Compute a [0,1] confidence grounded in genuine topical relevance.

    The store's fused score is rank-based, not an absolute-relevance scale (a
    small corpus pushes the top hit near 1.0 even for an off-topic query), so it
    cannot stand alone. We instead measure how many of the question's *meaningful*
    terms (stopwords removed) actually appear in the retrieved chunks, the
    fraction of the question that the knowledge base demonstrably covers, and
    weight it by the store's own confidence in the top hit. The result is high
    when retrieval returned on-topic content and low (→ escalate) when it did not.
    """

    if not scored:
        return 0.0

    # Primary signal: best cosine similarity of question vs any retrieved chunk.
    # Unlike the rank-based fusion score, this is absolute, so it can tell
    # "answerable" from "off-topic" and is what makes KAI escalate rather than
    # answer from a weakly-related chunk.
    best_sim = max((max(0.0, sc.vector_score) for sc in scored), default=0.0)
    best_sim = max(0.0, min(1.0, best_sim))

    # If a cross-encoder reranked the candidates, fold its (calibrated) relevance
    # into the primary signal. It scores (query, chunk) jointly and is far more
    # discriminative than cosine, especially at telling off-topic from on-topic.
    rerank_scores = [sc.rerank_score for sc in scored if sc.rerank_score is not None]
    if rerank_scores:
        best_rr = max(rerank_scores)
        # Map the cross-encoder score to a [0,1] relevance. ms-marco emits raw
        # logits → sigmoid. Models with a sigmoid head (e.g. bge-reranker-v2-m3)
        # already emit a 0-1 probability → use it directly (a second sigmoid would
        # compress the scale and inflate near-zero off-topic scores toward 0.5).
        if rerank_is_prob:
            if not (-0.01 <= best_rr <= 1.01):
                logger.warning(
                    "rerank_score %.3f is outside [0,1] but "
                    "rerank_score_is_probability=True, likely a reranker_model/flag "
                    "mismatch; confidence will be miscalibrated.",
                    best_rr,
                )
            ce_rel = max(0.0, min(1.0, best_rr))
        else:
            ce_rel = 1.0 / (1.0 + math.exp(-best_rr))  # sigmoid(best logit)
        relevance = 0.6 * ce_rel + 0.4 * best_sim
    else:
        relevance = best_sim

    # SECONDARY signal: lexical coverage of the question's content terms.
    q_tokens = _content_tokens(question)
    if q_tokens:
        retrieved_tokens: set[str] = set()
        for sc in scored:
            retrieved_tokens |= _content_tokens(f"{sc.chunk.title} {sc.chunk.text}")
        coverage = len(q_tokens & retrieved_tokens) / len(q_tokens)
    else:
        coverage = relevance

    # Relevance dominates; lexical overlap modulates it. A low relevance
    # (off-topic) keeps confidence low even if a few words coincidentally match.
    confidence = relevance * (0.5 + 0.5 * coverage)
    return max(0.0, min(1.0, confidence))


def _looks_like_idk(text: str) -> bool:
    """True if the model's text reads as an "I don't know" refusal.

    Tightened (was: any signal anywhere → many false positives). A refusal must
    START with a refusal opener (the prompt asks the model to reply exactly
    "I don't know"), OR be a short, UNCITED disclaimer. This stops a long,
    correctly-cited answer that mentions "the source does not contain X" in
    passing from being discarded as a refusal and needlessly escalated.
    """

    low = text.strip().lower()
    if not low:
        return True
    # Strip a leading hedge/wrapper before checking the opener.
    opener = low.lstrip("*_`\"' ")
    for hedge in _IDK_HEDGES:
        if opener.startswith(hedge):
            opener = opener[len(hedge) :].lstrip()
            break
    if opener.startswith(_IDK_PREFIXES):
        return True
    # Only consider refusal phrases when the reply is UNCITED: a genuine answer
    # carries a [n] citation (an answer that says "source [1] does not contain X"
    # in passing still passes, via the marker).
    if "[" not in low:
        # STRONG self-referential refusals fire on their own.
        if any(w in low for w in _IDK_STRONG):
            return True
        # WEAK phrases (which also appear in real content) only count when paired
        # with an anchor pointing at the sources/context, so a substantive answer
        # that merely says "the broker could not find a leader" is NOT discarded.
        if any(w in low for w in _IDK_WEAK) and any(a in low for a in _IDK_ANCHORS):
            return True
    return False


# Below this many source content-tokens the cited pages are too thin to cover a
# faithful answer's vocabulary (a 171-token source false-positived a correct answer
# at overlap 0.438), so the overlap guard is skipped. The other guards still apply.
_GROUNDING_MIN_SOURCE_TOKENS = 200


def _answer_grounding(answer: str, scored: list[ScoredChunk]) -> float:
    """Fraction of the answer's meaningful words that appear in the retrieved
    sources. A grounded answer reuses the sources' vocabulary (high); a fabricated
    answer pulled from the model's own knowledge does not (low). Deterministic and
    corpus-agnostic. Returns 1.0 for an empty answer (nothing to ground)."""

    a_tokens = _content_tokens(answer)
    if not a_tokens:
        return 1.0
    src_tokens: set[str] = set()
    for sc in scored:
        src_tokens |= _content_tokens(f"{sc.chunk.title} {sc.chunk.text}")
    return len(a_tokens & src_tokens) / len(a_tokens)


def _norm_alnum(s: str) -> str:
    """Lowercase, strip everything but [a-z0-9], for loose substring matching."""

    return re.sub(r"[^a-z0-9]", "", s.lower())


def _fabricated_specifics(answer: str, scored: list[ScoredChunk]) -> list[str]:
    """Concrete technical tokens in ``answer`` that are ABSENT from the sources.

    Returns the dotted identifiers / URIs the answer states that do not appear in
    any retrieved chunk (title + text). A non-empty result means the answer
    invented specifics from outside knowledge: the pipeline escalates instead of
    shipping them. Deterministic and corpus-agnostic; never consults the model
    (so it catches fabrication the model's own verify pass misses).
    """

    if not answer or not scored:
        return []
    src = " ".join(f"{sc.chunk.title} {sc.chunk.text}" for sc in scored).lower()
    # The dotted identifiers / URIs that ACTUALLY appear in the source, normalized.
    # A fabricated specific is "grounded" only when the source genuinely contains that
    # identifier: NOT when its letters happen to appear contiguously across separate
    # source words (the old blind alnum-substring test let "meta.data.cache" pass on
    # the prose "metadata cache").
    src_specifics: set[str] = {_norm_alnum(t) for t in _SPECIFIC_URI_RE.findall(src)}
    src_specifics |= {
        _norm_alnum(t) for t in _SPECIFIC_DOTTED_RE.findall(_SPECIFIC_URI_RE.sub(" ", src))
    }

    candidates: set[str] = set()
    # Collect config-style URIs first (skip http(s) doc links, low-risk and prone
    # to harmless reformatting), then STRIP all URIs from the text so a multi-part
    # domain inside a URL (e.g. ``sub.example.com``) isn't re-matched as a dotted
    # identifier.
    for tok in _SPECIFIC_URI_RE.findall(answer):
        if tok.lower().startswith(("http://", "https://")):
            continue
        candidates.add(tok)
    cleaned = _SPECIFIC_URI_RE.sub(" ", answer)
    for tok in _SPECIFIC_DOTTED_RE.findall(cleaned):
        if tok.lower().rstrip(".") in _SPECIFIC_IGNORE:
            continue
        candidates.add(tok)

    missing: list[str] = []
    for tok in candidates:
        norm = _norm_alnum(tok)
        if not norm:
            continue
        # Grounded ONLY if the source contains an identifier that normalizes the same
        # (whole-token match). A raw `tok in src` substring test is dropped. It let a
        # fabricated PREFIX of a real source identifier (e.g. "a.b.c" inside source's
        # "a.b.c.d") pass.
        if norm in src_specifics:
            continue
        missing.append(tok)
    return missing


# A number as a WHOLE token: thousands-grouped (1,234,567), a plain integer, an
# optional decimal, or scientific notation (1e9, 2.5e-3). A comma counts only when it
# sits between 3-digit groups, so a sentence comma ("3, 4, 5") is never glued on.
_NUMBER_RE = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?(?:[eE][+-]?\d+)?", re.ASCII)
# IPv4 addresses and dotted version strings (3+ components) are structure, not
# stated facts, mask them before extraction so octets / version parts aren't flagged.
_DOTTED_RUN_RE = re.compile(r"\bv?\d+(?:\.\d+){2,}\b", re.ASCII)


def _canon_num(s: str) -> str:
    """Canonicalize a comma-free numeric token so equal VALUES compare equal.

    "5000", "5000.0" and "5e3" all canonicalize to "5000"; "0.50" -> "0.5". This
    stops the source/answer number check from false-flagging a grounded number that
    the model merely reformatted (trailing zeros, decimal point, exponent).
    """

    if s.isdigit():
        # Pure integer, compare exactly. Python ints are arbitrary-precision, so a
        # huge value (> 2^53) keeps every digit instead of collapsing via float().
        return s.lstrip("0") or "0"
    try:
        f = float(s)
    except (ValueError, OverflowError):
        return s
    if not math.isfinite(f):
        return s  # e.g. "1e400" -> inf; keep the text rather than crash on int(inf)
    return str(int(f)) if f == int(f) else repr(f)


def _fabricated_numbers(answer: str, scored: list[ScoredChunk]) -> str | None:
    """A SIGNIFICANT number stated in ``answer`` that appears in NO source.

    Catches recombination fabrication: an invented total, count or measured value.
    "Significant" means a thousands separator, a decimal point, an exponent, or
    >= 5 integer digits, so years, ports and list ordinals don't trip it. Citation
    markers and IP/version strings are masked out of the answer first. Returns the
    offending token, or None when every number is grounded.
    """

    if not answer or not scored:
        return None
    text = _DOTTED_RUN_RE.sub(" ", re.sub(r"\[\d+\]", " ", answer))
    raw_src = " ".join(f"{sc.chunk.title} {sc.chunk.text}" for sc in scored)
    # Scan the source both masked and raw. Masking alone hid the components of a
    # version string, so an answer citing "1.15" against a source saying "1.15.0"
    # was flagged as fabricated. Digits present in the source text are grounded.
    src_canon = {
        _canon_num(m.replace(",", ""))
        for text_form in (_DOTTED_RUN_RE.sub(" ", raw_src), raw_src)
        for m in _NUMBER_RE.findall(text_form)
    }
    for tok in _NUMBER_RE.findall(text):
        norm = tok.replace(",", "")
        int_part = norm.split(".")[0].split("e")[0].split("E")[0]
        significant = "," in tok or "." in tok or "e" in tok.lower() or len(int_part) >= 5
        # Compare by VALUE, not text: "5,000" / "5000" / "5000.0" are the same number.
        if significant and norm and _canon_num(norm) not in src_canon:
            return tok
    return None


def _strip_trailing_idk(text: str) -> str:
    """Remove a contradictory trailing "I don't know" hedge from a substantive
    answer: the model sometimes appends one after giving real, cited information.
    Only strips trailing refusal lines; never empties a real answer."""

    lines = text.rstrip().splitlines()
    while lines and _looks_like_idk(lines[-1]):
        lines.pop()
    cleaned = "\n".join(lines).rstrip()
    return cleaned or text


# Editorialising openers the prompt forbids ("answer directly ... do NOT editorialize
# about what the sources do or do not contain") but a weaker model still emits.
# Stripped from the START of an answer only, longest-first.
# Fenced code blocks (``` ... ```, tolerant of an unterminated trailing fence) and
# inline backtick spans. Used to EXCLUDE code from prose tidying and citation
# rewriting, both were corrupting code (deleted (), [], collapsed indentation).
# re.split with the capture group keeps the code segments at odd indices.
_CODE_SEGMENT_RE = re.compile(r"(```.*?(?:```|$)|`[^`\n]+`)", re.DOTALL)

_EDITORIAL_OPENERS = (
    "based on the provided context sources,",
    "based on the provided context source,",
    "based on the provided context,",
    "based on the context provided,",
    "based on the provided context",
    "based on the context,",
    "according to the provided context,",
    "according to the context,",
    "based on the information provided,",
    "from the provided context,",
    "based on the sources provided,",
    "based on the given context,",
)


def _tidy_answer(text: str) -> str:
    """Clean up answer presentation without changing meaning.

    Drops a leading editorialising opener, collapses intra-line whitespace runs,
    and removes a space before sentence punctuation (artifacts from formatting or
    from dropping an out-of-range citation marker). Newlines/markdown are preserved.
    """

    t = (text or "").strip()
    low = t.lower()
    for opener in _EDITORIAL_OPENERS:
        if low.startswith(opener):
            t = t[len(opener) :].lstrip(" ,:-")
            if t:
                t = t[0].upper() + t[1:]
            break
    # Tidy PROSE only, code must pass through untouched. Fenced blocks and inline
    # backtick spans are split out first; the cleanup rules run on the prose
    # segments and the pieces are reassembled. Inside prose, rules are scoped so
    # legitimate code-like text survives even unfenced: indentation (leading
    # whitespace) is never collapsed, call parens (``f()``) are never removed, and
    # newlines are never folded into punctuation joins.
    out: list[str] = []
    for i, seg in enumerate(_CODE_SEGMENT_RE.split(t)):
        if i % 2 == 1:  # a fenced block / inline code span, verbatim
            out.append(seg)
            continue
        seg = re.sub(r"(?<=\S)[ \t]{2,}", " ", seg)  # intra-line runs (not indentation)
        seg = re.sub(r"[ \t]+([.,;:!?])", r"\1", seg)  # space/tab before punctuation (not \n)
        # Standalone empty brackets/parens (citation-drop artifacts): only when
        # free-standing: never glued to an identifier like ``f()`` or ``items[]``.
        seg = re.sub(r"(?<!\S)\(\s*\)(?=\s|$|[.,;:!?])", "", seg)
        seg = re.sub(r"(?<!\S)\[\s*\](?=\s|$|[.,;:!?])", "", seg)
        seg = re.sub(r"(?<!\.)\.\.(?!\.)", ".", seg)  # stray double period -> one
        seg = re.sub(r"(?<=\S)[ \t]{2,}", " ", seg)  # re-collapse if removals left gaps
        out.append(seg)
    return "".join(out).strip()


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def _unsupported_sentences(
    answer: str,
    scored: list[ScoredChunk],
    embedder: Embedder,
    floor: float,
    query_prefix: str = "",
    passage_prefix: str = "",
) -> list[str]:
    """Prose sentences of ``answer`` not semantically supported by any source.

    Sentence-level grounding: each substantive prose sentence (code
    segments excluded, >=6 content tokens) must reach cosine >= ``floor``
    against at least one retrieved chunk. Sentences + chunks are embedded in
    ONE batched call. Returns the unsupported sentences (empty = grounded).
    """

    prose = " ".join(
        seg for i, seg in enumerate(_CODE_SEGMENT_RE.split(answer or "")) if i % 2 == 0
    )
    sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(prose) if len(_content_tokens(s)) >= 6]
    if not sentences or not scored:
        return []

    chunk_texts = [f"{sc.chunk.title}. {sc.chunk.text}" for sc in scored]
    vectors = embedder.embed(
        [f"{query_prefix}{s}" for s in sentences] + [f"{passage_prefix}{t}" for t in chunk_texts]
    )
    sent_vecs, chunk_vecs = vectors[: len(sentences)], vectors[len(sentences) :]

    def _cos(a, b) -> float:  # noqa: ANN001 - float sequences
        dot = sum(x * y for x, y in zip(a, b, strict=True))
        na = math.sqrt(sum(x * x for x in a)) or 1.0
        nb = math.sqrt(sum(y * y for y in b)) or 1.0
        return dot / (na * nb)

    unsupported: list[str] = []
    for s, sv in zip(sentences, sent_vecs, strict=True):
        best = max((_cos(sv, cv) for cv in chunk_vecs), default=0.0)
        if best < floor:
            unsupported.append(s)
    return unsupported


def _citations_from(scored: list[ScoredChunk]) -> list[Citation]:
    """Build de-duplicated (by URL) citations from the chunks used."""

    citations: list[Citation] = []
    seen: set[str] = set()
    for sc in scored:
        url = sc.chunk.url.strip()
        key = url or f"title::{sc.chunk.title}"
        if key in seen:
            continue
        seen.add(key)
        citations.append(Citation(title=sc.chunk.title, url=url))
    return citations


# A trailing "Sources:" / "References:" list the model sometimes appends despite the
# inline-citation instruction, bare [n] markers (often duplicated, e.g. "[1] [2] [2]")
# that the UI and chat layers would then render a SECOND time. We strip it; the real,
# de-duplicated Sources list is built from the citations, not the model's prose.
_SOURCES_HEADING_RE = re.compile(
    r"^[#>*_\s-]*(?:sources?|references?|citations?)[\s:*_#-]*$", re.IGNORECASE
)
_REF_LINE_RE = re.compile(r"^[-*]?\s*\[\d+\]")  # "[1]", "[1] Title", "- [2] ..."
_BARE_MARKER_RE = re.compile(r"^\[\d+\]$")  # exactly "[1]"


def _strip_model_sources_block(text: str) -> str:
    """Drop a trailing model-emitted 'Sources:'/'References:' list of ``[n]`` markers.

    The answer cites INLINE (``[1]`` within a sentence); a standalone trailing
    reference list is redundant with the de-duplicated Sources the UI/chat render from
    the citations, and often arrives mangled (e.g. ``[1] [2] [2]``). Only a trailing
    block under a Sources heading, OR a trailing run of *bare* ``[n]`` lines, is
    removed, prose (including a sentence that merely starts with a marker) is left
    untouched.
    """

    if not text:
        return text
    lines = text.split("\n")
    end = len(lines)
    while end > 0 and not lines[end - 1].strip():  # trim trailing blank lines
        end -= 1
    start = end
    while start > 0 and _REF_LINE_RE.match(lines[start - 1].strip()):
        start -= 1
    if start == end:
        return text  # no trailing reference lines → nothing to strip
    heading = start
    while heading > 0 and not lines[heading - 1].strip():
        heading -= 1
    has_heading = heading > 0 and _SOURCES_HEADING_RE.match(lines[heading - 1].strip())
    all_bare = all(_BARE_MARKER_RE.match(lines[k].strip()) for k in range(start, end))
    if not (has_heading or all_bare):
        return text  # titled refs with no heading → ambiguous, leave it alone
    cut = (heading - 1) if has_heading else start
    return "\n".join(lines[:cut]).rstrip()


def _finalize_citations(raw_answer: str, scored: list[ScoredChunk]) -> tuple[str, list[Citation]]:
    """Renumber the answer's ``[n]`` markers to match the de-duplicated sources.

    The prompt numbers every retrieved chunk 1..N, but a long page is split into
    many chunks, so a thorough answer can carry markers like ``[1]..[8]`` that all
    resolve to the SAME source URL: which made the answer look like it had eight
    references next to a single Sources link. Here we collapse the cited chunks to
    their UNIQUE sources (by URL) and rewrite the inline markers so the numbers in
    the answer correspond 1:1 to the Sources list, numbered in the order the
    sources first appear reading left-to-right. Markers whose number exceeds the
    source count (a hallucinated reference) are dropped. Returns ``(answer,
    citations)``.

    Falls back to the single top-ranked source (text untouched) when the model
    emitted no usable markers, so a confident answer always carries one citation.
    """

    # Drop any trailing "Sources:"/"References:" list the model tacked on: the real
    # Sources block is rendered from the citations below, so the model's copy is just
    # redundant (and often mangled, e.g. "[1] [2] [2]") clutter.
    raw_answer = _strip_model_sources_block(raw_answer or "")

    # Work over PROSE segments only, bracketed numbers inside code (``ports[0]``,
    # fenced examples) are array indices, not citations, and must never be deleted
    # or renumbered. Within prose, a ``[n]`` counts as a citation marker only when
    # it is NOT glued to an identifier (``argv[1]``), except that a marker may
    # directly follow another marker (``[1][2]``), which models legitimately emit.
    segments = _CODE_SEGMENT_RE.split(raw_answer)

    def _markers(seg: str) -> list[tuple[int, int, int]]:
        """(start, end, n) for each bracketed number in ``seg`` that is a citation.

        Code/inline-backtick spans are already split out, so within PROSE a ``[n]`` is
        a citation when it is NOT glued to a word char (the normal ``... [1]``), OR it
        chains another marker (``[1][2]``), OR it is glued but IN RANGE (``Kafka[1]``,
        which models emit). A glued, OUT-OF-RANGE ``[n]`` is almost certainly an array
        index in unfenced prose (``arr[0]``, ``items[99]``), left untouched.
        """

        found: list[tuple[int, int, int]] = []
        last_end = -1
        for mo in re.finditer(r"\[(\d+)\]", seg):
            n = int(mo.group(1))
            prev = seg[mo.start() - 1] if mo.start() > 0 else " "
            glued = prev.isalnum() or prev == "_"
            chained = mo.start() == last_end  # directly follows another marker ([1][2])
            if (not glued) or chained or (1 <= n <= len(scored)):
                found.append((mo.start(), mo.end(), n))
                last_end = mo.end()
        return found

    per_seg = [_markers(seg) if i % 2 == 0 else [] for i, seg in enumerate(segments)]
    valid = {n for ms in per_seg for (_, _, n) in ms if 1 <= n <= len(scored)}
    if not valid:
        # No usable [n] markers, but there may be out-of-range phantom markers; drop
        # them so they don't show next to the single fallback citation. Cite the
        # source whose text best overlaps the answer (not merely the top-ranked one).
        cleaned_parts = []
        for i, seg in enumerate(segments):
            ms = per_seg[i]
            if not ms:
                cleaned_parts.append(seg)
                continue
            out, last = [], 0
            for s, e, _n in ms:
                out.append(seg[last:s])
                last = e
            out.append(seg[last:])
            cleaned_parts.append("".join(out))
        cleaned = "".join(cleaned_parts)
        ans_tokens = _content_tokens(cleaned)
        best = (
            max(
                scored,
                key=lambda sc: len(
                    ans_tokens & _content_tokens(f"{sc.chunk.title} {sc.chunk.text}")
                ),
            )
            if scored
            else None
        )
        return cleaned, _citations_from([best] if best is not None else scored[:1])

    def _key(sc: ScoredChunk) -> str:
        return sc.chunk.url.strip() or f"title::{sc.chunk.title}"

    remap: dict[int, int] = {}  # original chunk number -> compact source index
    key_to_idx: dict[str, int] = {}  # unique source key -> compact index
    citations: list[Citation] = []
    for ms in per_seg:
        for _, _, n in ms:
            if n not in valid:
                continue
            sc = scored[n - 1]
            key = _key(sc)
            if key not in key_to_idx:
                key_to_idx[key] = len(citations) + 1
                citations.append(Citation(title=sc.chunk.title, url=sc.chunk.url.strip()))
            remap[n] = key_to_idx[key]

    rewritten_segments: list[str] = []
    for i, seg in enumerate(segments):
        ms = per_seg[i]
        if not ms:
            rewritten_segments.append(seg)
            continue
        parts: list[str] = []
        cursor = 0
        for start, end, n in ms:
            parts.append(seg[cursor:start])
            if n in remap:
                parts.append(f"[{remap[n]}]")
            # else: an out-of-range CITATION marker (hallucinated ref), drop it.
            # Non-citation brackets were never classified, so code/indices survive.
            cursor = end
        parts.append(seg[cursor:])
        rewritten_segments.append("".join(parts))
    return "".join(rewritten_segments), citations


def _escalate(
    question: str,
    scored: list[ScoredChunk],
    raw_answer: str,
    confidence: float,
    tracker: Tracker,
    *,
    include_draft: bool = False,
) -> Answer:
    """Open a tracker issue and return an escalated :class:`Answer`."""

    body = _escalation_body(question, scored, raw_answer, confidence, include_draft=include_draft)
    # A single-line title: tracker summaries (Jira) reject embedded newlines, and a
    # pasted multi-line question must never make the escalation itself fail.
    title = "KAI could not answer: " + " ".join(question.split())
    try:
        escalation_url = (tracker.create_issue(title=title, body=body) or "").strip()
    except Exception as exc:  # noqa: BLE001 - the safety-net path must never 500
        # Tracker outage (e.g. Jira down/4xx): degrade to the no-URL message, the
        # user still gets the correct escalated answer, and the failure is logged
        # loudly for the operator instead of surfacing as an HTTP 500.
        logger.error(
            "kai_escalation_failed err=%s: %s, escalation NOT ticketed: %r",
            type(exc).__name__,
            exc,
            question[:160],
        )
        escalation_url = ""
    if escalation_url:
        message = (
            "I couldn't answer this confidently from the knowledge base, so I've "
            f"raised a ticket for a human to follow up: {escalation_url}"
        )
    else:
        # No external tracker wired (LocalTracker): no fake link.
        message = (
            "I couldn't answer this confidently from the knowledge base, so I've "
            "flagged it for a human to review."
        )
    return Answer(
        # An escalated answer did NOT answer the question, so it cites nothing.
        # The closest-but-insufficient sources are also in the ticket body for the
        # human; ``suggested_sources`` surfaces them to the ASKER (clearly labeled
        # not-an-answer by the renderer) so escalation isn't a dead end.
        answer=message,
        citations=[],
        confidence=confidence,
        escalated=True,
        escalation_url=escalation_url or None,
        suggested_sources=_citations_from(scored[:3]),
    )


def _escalation_body(
    question: str,
    scored: list[ScoredChunk],
    raw_answer: str,
    confidence: float,
    *,
    include_draft: bool = False,
) -> str:
    """Render the human-readable escalation ticket body (plain text)."""

    lines = [
        "KAI was unable to answer the following question with sufficient confidence.",
        "",
        f"Question: {question}",
        f"Confidence: {confidence:.3f}",
        "",
    ]
    if scored:
        lines.append("Closest knowledge-base sources retrieved:")
        for i, sc in enumerate(scored, start=1):
            url = sc.chunk.url or "(no url)"
            lines.append(f"  {i}. {sc.chunk.title}, {url} (score {sc.score:.3f})")
    else:
        lines.append("No relevant knowledge-base sources were retrieved.")
    # M11 egress boundary: the UNVERIFIED model draft goes to an (external)
    # tracker only when explicitly enabled, see escalation_include_draft.
    if raw_answer and include_draft:
        lines += ["", "Model draft (not surfaced to the user):", raw_answer]
    return "\n".join(lines)

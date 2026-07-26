"""End-to-end smoke tests against a real LLM, real embeddings and a real database.

This is the only suite that exercises the actual promise: a question the corpus
covers gets a cited answer, and a question it does not cover escalates instead of
being fabricated. Everything else in the test tree uses fakes.

Run it with both services present, for example against Ollama:

    KAI_TEST_DATABASE_URL=postgresql://postgres:root@localhost:5432/kai_test \
    KAI_TEST_LLM_BASE_URL=http://localhost:11434/v1 \
    pytest -q tests/integration/test_live_llm.py

Model choices default to qwen2.5:14b-instruct + nomic-embed-text and can be
overridden with KAI_TEST_LLM_MODEL / KAI_TEST_EMBED_MODEL.
"""

from __future__ import annotations

import pytest

from kai.config import Settings
from kai.interfaces import Doc
from kai.pipeline.ask import answer_question
from kai.pipeline.ingest import ingest_from
from kai.providers.embedding_openai import OpenAIEmbedder
from kai.providers.llm_openai import OpenAILLM
from kai.providers.vectorstore_pgvector import PgVectorStore

pytestmark = [pytest.mark.integration, pytest.mark.live_llm]

# A small corpus with facts a general-purpose model does NOT know, so a confident
# answer can only have come from retrieval.
CORPUS = [
    Doc(
        id="vpn",
        title="VPN access guide",
        url="https://kb.example/vpn",
        html=(
            "To request VPN access, open a ticket in the ACME service desk portal under "
            "the 'Network Access' category. Approval is granted by the Platform Security "
            "team and normally completes within one business day. Once approved you "
            "receive an enrolment email containing a one-time token that is valid for "
            "72 hours. Install the AcmeConnect client, paste the token, and choose the "
            "eu-west gateway. VPN sessions are capped at 12 hours and re-authentication "
            "requires the hardware key registered during onboarding."
        ),
        space="test",
        content_type="text",
    ),
    Doc(
        id="oncall",
        title="On-call runbook",
        url="https://kb.example/oncall",
        html=(
            "The primary on-call engineer must acknowledge a page within 15 minutes. "
            "If the page is not acknowledged, PagerDuty escalates to the secondary "
            "on-call, and after a further 10 minutes to the engineering manager. "
            "Sev-1 incidents require a status page update within 30 minutes of "
            "acknowledgement and a written postmortem within five working days. "
            "Handover happens every Monday at 10:00 UTC in the #ops-handover channel."
        ),
        space="test",
        content_type="text",
    ),
]


@pytest.fixture(scope="module")
def live_providers(live_llm_config: dict):
    embedder = OpenAIEmbedder(
        base_url=live_llm_config["embed_base_url"],
        api_key=live_llm_config["api_key"],
        model=live_llm_config["embed_model"],
        dimensions=live_llm_config["embed_dimensions"],
        timeout=180,
    )
    llm = OpenAILLM(
        base_url=live_llm_config["base_url"],
        api_key=live_llm_config["api_key"],
        model=live_llm_config["llm_model"],
        timeout=180,
    )
    yield embedder, llm
    embedder.close()
    llm.close()


@pytest.fixture(scope="module")
def live_store(request, live_providers, live_llm_config):
    """Ingest CORPUS once for the module, into a table dropped afterwards."""

    import os
    import uuid

    import psycopg

    db = (os.environ.get("KAI_TEST_DATABASE_URL") or "").strip()
    if not db:
        pytest.skip("set KAI_TEST_DATABASE_URL to run the live-LLM suite")

    embedder, _llm = live_providers
    table = f"kai_live_{uuid.uuid4().hex[:12]}"
    store = PgVectorStore(database_url=db, table=table)

    class _Source:
        errors = 0

        def __init__(self) -> None:
            self.seen_ids: set[str] = set()

        def iter_pages(self):
            yield from CORPUS

    written = ingest_from(embedder, store, _Source(), passage_prefix="search_document: ")
    assert written > 0, "the live corpus produced no chunks"

    def _drop() -> None:
        with psycopg.connect(db) as conn:
            with conn.cursor() as cur:
                cur.execute(f'DROP TABLE IF EXISTS "{table}"')
                cur.execute(f'DROP TABLE IF EXISTS "{table}_hashes"')
            conn.commit()

    request.addfinalizer(_drop)
    return store


class _NoTracker:
    def create_issue(self, title: str, body: str) -> str:
        return ""


def _settings(**over) -> Settings:
    base = {
        "reranker": "noop",
        "multi_query": False,
        "query_rewrite": False,
        "verify_answers": True,
        "sentence_grounding": False,
        "confidence_threshold": 0.45,
        "answer_grounding_min": 0.48,
        "top_k": 5,
        "database_url": "",
        "embed_query_prefix": "search_query: ",
        "embed_passage_prefix": "search_document: ",
    }
    base.update(over)
    return Settings(_env_file=None, **base)


def test_embeddings_have_the_configured_width(live_providers, live_llm_config) -> None:
    embedder, _llm = live_providers
    vectors = embedder.embed(["search_query: how do I request VPN access"])
    assert len(vectors) == 1
    assert len(vectors[0]) == live_llm_config["embed_dimensions"]


def test_the_llm_answers_a_trivial_prompt(live_providers) -> None:
    _embedder, llm = live_providers
    out = llm.complete("Reply with exactly one word.", "Say OK.", max_tokens=8, temperature=0.0)
    assert out.strip(), "the live model returned an empty completion"


@pytest.mark.parametrize(
    ("question", "must_mention"),
    [
        ("How do I request VPN access?", ("service desk", "ticket", "portal")),
        ("How long does the on-call engineer have to acknowledge a page?", ("15", "fifteen")),
    ],
)
def test_an_in_scope_question_is_answered_with_a_citation(
    live_store, live_providers, question, must_mention
) -> None:
    embedder, llm = live_providers
    answer = answer_question(question, embedder, llm, live_store, _NoTracker(), _settings())

    assert not answer.escalated, f"in-scope question escalated: {answer.answer!r}"
    assert answer.citations, "a confident answer must carry at least one citation"
    assert all(c.url for c in answer.citations)
    low = answer.answer.lower()
    assert any(m.lower() in low for m in must_mention), (
        f"answer did not draw on the source: {answer.answer!r}"
    )


@pytest.mark.parametrize(
    "question",
    [
        "What is the annual revenue of the Zorblatt Corporation?",
        "How do I configure the quantum flux capacitor in our billing system?",
    ],
)
def test_an_out_of_scope_question_escalates_rather_than_fabricating(
    live_store, live_providers, question
) -> None:
    embedder, llm = live_providers
    answer = answer_question(question, embedder, llm, live_store, _NoTracker(), _settings())

    assert answer.escalated, f"out-of-scope question was answered: {answer.answer!r}"
    assert answer.citations == [], "an escalation must cite nothing"


def test_a_plausible_but_uncovered_detail_is_not_invented(live_store, live_providers) -> None:
    """The corpus covers VPN enrolment but says nothing about pricing. A model
    filling that gap from general knowledge is exactly what the guards exist for."""

    embedder, llm = live_providers
    answer = answer_question(
        "How much does an AcmeConnect VPN licence cost per user per year?",
        embedder,
        llm,
        live_store,
        _NoTracker(),
        _settings(),
    )

    assert answer.escalated, f"invented a price: {answer.answer!r}"


def test_the_same_question_twice_gives_the_same_verdict(live_store, live_providers) -> None:
    """temperature=0 throughout, so the answer/escalate decision must be stable."""

    embedder, llm = live_providers
    question = "How do I request VPN access?"
    first = answer_question(question, embedder, llm, live_store, _NoTracker(), _settings())
    second = answer_question(question, embedder, llm, live_store, _NoTracker(), _settings())

    assert first.escalated == second.escalated
    assert first.confidence == pytest.approx(second.confidence, abs=1e-6)

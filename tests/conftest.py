"""Shared fixtures.

The unit suite runs on fakes and needs no services. The integration suite needs a
real Postgres with pgvector; it is skipped unless ``KAI_TEST_DATABASE_URL`` is set:

    KAI_TEST_DATABASE_URL=postgresql://kai:kai@localhost:5432/kai_test pytest

The live-LLM smoke tests need an OpenAI-compatible endpoint and are skipped unless
``KAI_TEST_LLM_BASE_URL`` is set.
"""

from __future__ import annotations

import os
import uuid

import pytest

INTEGRATION_DB_ENV = "KAI_TEST_DATABASE_URL"
LIVE_LLM_ENV = "KAI_TEST_LLM_BASE_URL"


@pytest.fixture(scope="session")
def integration_db_url() -> str:
    """DSN of a live Postgres with pgvector, or skip the test."""

    url = (os.environ.get(INTEGRATION_DB_ENV) or "").strip()
    if not url:
        pytest.skip(f"set {INTEGRATION_DB_ENV} to run the integration suite")
    return url


@pytest.fixture
def pg_table(integration_db_url: str):
    """A unique, empty table name; dropped (with its hashes side table) afterwards.

    Per-test tables let the integration cases run in any order and in parallel
    without one test's rows changing another's search results.
    """

    import psycopg

    name = f"kai_it_{uuid.uuid4().hex[:12]}"
    yield name
    with psycopg.connect(integration_db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(f'DROP TABLE IF EXISTS "{name}"')
            cur.execute(f'DROP TABLE IF EXISTS "{name}_hashes"')
        conn.commit()


@pytest.fixture(scope="session")
def live_llm_config() -> dict:
    """Base URL / model / dimensions for a live OpenAI-compatible endpoint, or skip."""

    base = (os.environ.get(LIVE_LLM_ENV) or "").strip()
    if not base:
        pytest.skip(f"set {LIVE_LLM_ENV} to run the live-LLM smoke tests")
    return {
        "base_url": base,
        "api_key": os.environ.get("KAI_TEST_LLM_API_KEY", "ollama"),
        "llm_model": os.environ.get("KAI_TEST_LLM_MODEL", "qwen2.5:14b-instruct"),
        "embed_base_url": os.environ.get("KAI_TEST_EMBED_BASE_URL", base),
        "embed_model": os.environ.get("KAI_TEST_EMBED_MODEL", "nomic-embed-text"),
        "embed_dimensions": int(os.environ.get("KAI_TEST_EMBED_DIMENSIONS", "768")),
    }

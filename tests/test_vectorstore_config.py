"""The vector store must validate vector_type (switch between vector/halfvec)."""

import pytest

from kai.providers.vectorstore_pgvector import PgVectorStore

DB = "postgresql://u@localhost/x"  # not connected to, validation is in __init__


def test_valid_vector_types_construct():
    assert PgVectorStore(DB, "kai_chunks", vector_type="vector")._vtype == "vector"
    assert PgVectorStore(DB, "kai_chunks", vector_type="halfvec")._vtype == "halfvec"
    assert PgVectorStore(DB, "kai_chunks", vector_type="HALFVEC")._vtype == "halfvec"


def test_invalid_vector_type_rejected():
    with pytest.raises(ValueError):
        PgVectorStore(DB, "kai_chunks", vector_type="float8")


def test_default_is_float32_vector():
    assert PgVectorStore(DB, "kai_chunks")._vtype == "vector"

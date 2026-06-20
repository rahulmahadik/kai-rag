"""nomic embedding task prefixes are auto-filled for nomic models only.

`search_query:` / `search_document:` are mandatory for nomic-embed-text; other
models must stay unprefixed unless the operator sets them explicitly.
"""

from kai.config import Settings


def test_nomic_model_gets_task_prefixes():
    s = Settings(_env_file=None, embed_model="nomic-embed-text")
    assert s.embed_query_prefix == "search_query: "
    assert s.embed_passage_prefix == "search_document: "


def test_non_nomic_model_has_no_prefix():
    s = Settings(_env_file=None, embed_model="text-embedding-3-small")
    assert s.embed_query_prefix == ""
    assert s.embed_passage_prefix == ""


def test_explicit_prefix_is_not_overridden():
    s = Settings(
        _env_file=None,
        embed_model="nomic-embed-text",
        embed_query_prefix="custom_query: ",
        embed_passage_prefix="custom_doc: ",
    )
    assert s.embed_query_prefix == "custom_query: "
    assert s.embed_passage_prefix == "custom_doc: "

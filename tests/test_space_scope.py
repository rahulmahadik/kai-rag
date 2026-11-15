"""Per-space answer scoping (ASK_SPACES / ASK_SPACE_ACL / request spaces)."""

from __future__ import annotations

from kai.config import Settings
from kai.pipeline.inform import CURATED_SPACE
from kai.pipeline.scope import parse_space_acl, resolve_spaces, search_filters


def _s(**over) -> Settings:
    return Settings(_env_file=None, **over)


def test_no_config_means_no_filter() -> None:
    assert resolve_spaces(_s()) is None
    assert resolve_spaces(_s(), requested=["ENG"]) == ["ENG", CURATED_SPACE]


def test_global_ask_spaces() -> None:
    spaces = resolve_spaces(_s(ask_spaces="ENG, OPS"))
    assert spaces == ["ENG", "OPS", CURATED_SPACE]


def test_request_cannot_expand_past_allowlist() -> None:
    spaces = resolve_spaces(_s(ask_spaces="ENG"), requested=["ENG", "SECRET"])
    assert spaces == ["ENG", CURATED_SPACE]


def test_acl_exact_and_domain() -> None:
    acl = "alice@acme.com:ENG,OPS;@acme.com:ENG;U123:HR"
    assert parse_space_acl(acl)["alice@acme.com"] == ["ENG", "OPS"]
    alice = resolve_spaces(_s(ask_space_acl=acl), identity="Alice@Acme.com")
    assert alice == ["ENG", "OPS", CURATED_SPACE]
    bob = resolve_spaces(_s(ask_space_acl=acl), identity="bob@acme.com")
    assert bob == ["ENG", CURATED_SPACE]
    slack = resolve_spaces(_s(ask_space_acl=acl), identity="U123")
    assert slack == ["HR", CURATED_SPACE]


def test_acl_unknown_identity_is_denied() -> None:
    assert resolve_spaces(_s(ask_space_acl="a@b.co:ENG"), identity="eve@x.co") == []
    assert resolve_spaces(_s(ask_space_acl="a@b.co:ENG"), identity="") == []


def test_curated_can_be_disabled() -> None:
    spaces = resolve_spaces(_s(ask_spaces="ENG", ask_include_curated=False))
    assert spaces == ["ENG"]
    assert CURATED_SPACE not in spaces


def test_search_filters_helper() -> None:
    assert search_filters(None) is None
    assert search_filters(["ENG"]) == {"space": ["ENG"]}
    assert search_filters([]) == {"space": []}


def test_retrieve_passes_space_filter(monkeypatch) -> None:
    from kai.interfaces import Chunk, ScoredChunk
    from kai.pipeline import ask as ask_mod

    seen: dict = {}

    class _Embed:
        dimensions = 2

        def embed(self, texts):
            return [[0.1, 0.2] for _ in texts]

    class _LLM:
        def complete(self, *a, **k):
            return ""

    class _Store:
        def search(self, query_vector, query_text, top_k, filters=None):
            seen["filters"] = filters
            return [
                ScoredChunk(
                    chunk=Chunk(
                        id="1",
                        doc_id="d",
                        title="t",
                        url="",
                        space="ENG",
                        ordinal=0,
                        text="hello",
                    ),
                    score=1.0,
                    vector_score=0.9,
                )
            ]

    settings = _s(ask_spaces="ENG", reranker="noop", multi_query=False, query_rewrite=False)
    q, scored = ask_mod.retrieve("how?", _Embed(), _LLM(), _Store(), settings, asker="anyone")
    assert q == "how?"
    assert seen["filters"] == {"space": ["ENG", CURATED_SPACE]}
    assert len(scored) == 1


def test_retrieve_denies_empty_acl_without_embedding() -> None:
    from kai.pipeline import ask as ask_mod

    class _BoomEmbed:
        dimensions = 2

        def embed(self, texts):
            raise AssertionError("must not embed when denied")

    class _LLM:
        def complete(self, *a, **k):
            return ""

    class _Store:
        def search(self, *a, **k):
            raise AssertionError("must not search when denied")

    settings = _s(ask_space_acl="a@b.co:ENG", reranker="noop")
    q, scored = ask_mod.retrieve("how?", _BoomEmbed(), _LLM(), _Store(), settings, asker="eve@x.co")
    assert q == "how?" and scored == []

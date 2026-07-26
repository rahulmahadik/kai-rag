"""The HTTP providers, driven through a mock transport.

The LLM, embedding, Jira and Confluence clients are where a bad response shape,
a 4xx or a network error has to be turned into something the pipeline can act on.
httpx's MockTransport lets every one of those branches run without a network.
"""

from __future__ import annotations

import httpx
import pytest

from kai.config import Settings
from kai.providers.confluence_cloud import ConfluenceCloudKBSource
from kai.providers.embedding_openai import OpenAIEmbedder
from kai.providers.jira_cloud import JiraCloudTracker
from kai.providers.llm_openai import OpenAILLM

_REAL_CLIENT = httpx.Client


def _mock(handler) -> httpx.Client:
    return _REAL_CLIENT(transport=httpx.MockTransport(handler))


def _patch_client(monkeypatch, handler) -> None:
    """Route every httpx.Client(...) built inside a provider through ``handler``.

    Jira and Confluence open their client per call rather than holding one, so the
    class itself is the injection point. _REAL_CLIENT is captured before patching so
    the replacement does not call itself.
    """

    monkeypatch.setattr(httpx, "Client", lambda **kw: _mock(handler))


# ======================================================================= #
# OpenAILLM
# ======================================================================= #
def _llm(handler=None) -> OpenAILLM:
    llm = OpenAILLM(base_url="http://x/v1", api_key="k", model="m", timeout=5)
    if handler is not None:
        llm._http = _mock(handler)
    return llm


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"base_url": " "}, "llm_base_url"),
        ({"api_key": " "}, "llm_api_key"),
        ({"model": " "}, "llm_model"),
    ],
)
def test_llm_rejects_blank_required_config(kwargs, match) -> None:
    base = {"base_url": "http://x/v1", "api_key": "k", "model": "m"}
    with pytest.raises(ValueError, match=match):
        OpenAILLM(**{**base, **kwargs})


def test_llm_returns_the_assistant_content() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/chat/completions")
        assert request.headers["Authorization"] == "Bearer k"
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})

    assert _llm(handler).complete("sys", "user") == "hi"


def test_llm_sends_the_model_and_sampling_parameters() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    _llm(handler).complete("sys", "user", max_tokens=32, temperature=0.0)

    assert seen["model"] == "m"
    assert seen["max_tokens"] == 32
    assert seen["temperature"] == 0.0
    assert [m["role"] for m in seen["messages"]] == ["system", "user"]


@pytest.mark.parametrize("status", [400, 401, 429, 500, 503])
def test_llm_raises_on_a_non_200_without_leaking_the_body(status) -> None:
    llm = _llm(lambda r: httpx.Response(status, text="secret-space-key-ENG"))
    with pytest.raises(RuntimeError) as exc:
        llm.complete("s", "u")
    assert str(status) in str(exc.value)
    assert "secret-space-key" not in str(exc.value)


def test_llm_raises_on_a_network_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(RuntimeError, match="ConnectError"):
        _llm(handler).complete("s", "u")


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ({"choices": []}, "choices"),
        ({"choices": [{}]}, "message"),
        ({"choices": [{"message": {}}]}, "content"),
        ({"choices": [{"message": {"content": 42}}]}, "content"),
        ({}, "choices"),
    ],
)
def test_llm_raises_on_a_malformed_response(body, match) -> None:
    with pytest.raises(RuntimeError, match=match):
        _llm(lambda r: httpx.Response(200, json=body)).complete("s", "u")


def test_llm_raises_on_non_json() -> None:
    with pytest.raises(RuntimeError, match="non-JSON"):
        _llm(lambda r: httpx.Response(200, text="<html>")).complete("s", "u")


def test_llm_reuses_one_client_and_close_is_idempotent() -> None:
    llm = _llm()
    assert llm._client() is llm._client()
    llm.close()
    llm.close()
    assert llm._http is None


def test_llm_from_settings_reads_the_llm_fields() -> None:
    settings = Settings(
        _env_file=None,
        llm_base_url="http://host/v1/",
        llm_api_key="key",
        llm_model="model",
        llm_timeout=42,
    )
    llm = OpenAILLM.from_settings(settings)
    assert llm._base_url == "http://host/v1"  # trailing slash normalised
    assert llm._timeout == 42


# ======================================================================= #
# OpenAIEmbedder
# ======================================================================= #
def _embedder(handler=None, dimensions=3) -> OpenAIEmbedder:
    emb = OpenAIEmbedder(
        base_url="http://x/v1", api_key="k", model="m", dimensions=dimensions, timeout=5
    )
    if handler is not None:
        emb._http = _mock(handler)
    return emb


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"base_url": " "}, "embed_base_url"),
        ({"api_key": " "}, "embed_api_key"),
        ({"model": " "}, "embed_model"),
        ({"dimensions": 0}, "embed_dimensions"),
        ({"dimensions": -1}, "embed_dimensions"),
    ],
)
def test_embedder_rejects_bad_config(kwargs, match) -> None:
    base = {"base_url": "http://x/v1", "api_key": "k", "model": "m", "dimensions": 3}
    with pytest.raises(ValueError, match=match):
        OpenAIEmbedder(**{**base, **kwargs})


def test_embedder_returns_one_vector_per_input() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        n = len(json.loads(request.content)["input"])
        return httpx.Response(
            200, json={"data": [{"index": i, "embedding": [0.0, 0.0, float(i)]} for i in range(n)]}
        )

    out = _embedder(handler).embed(["a", "b"])
    assert out == [[0.0, 0.0, 0.0], [0.0, 0.0, 1.0]]


def test_embedder_returns_early_for_an_empty_batch() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json={"data": []})

    assert _embedder(handler).embed([]) == []
    assert calls == [], "an empty batch must not hit the network"


def test_embedder_reorders_an_out_of_order_response_by_index() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [1.0, 1.0, 1.0]},
                    {"index": 0, "embedding": [0.0, 0.0, 0.0]},
                ]
            },
        )

    assert _embedder(handler).embed(["a", "b"]) == [[0.0, 0.0, 0.0], [1.0, 1.0, 1.0]]


def test_embedder_retries_a_rejected_batch_one_item_at_a_time() -> None:
    """A short-context model 4xxs the whole batch; one oversized chunk must not
    fail the rest of an ingest."""

    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        items = json.loads(request.content)["input"]
        seen.append(len(items))
        if len(items) > 1:
            return httpx.Response(400, json={"error": "too long"})
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0, 0.0, 0.0]}]})

    out = _embedder(handler).embed(["a", "b"])

    assert out == [[1.0, 0.0, 0.0], [1.0, 0.0, 0.0]]
    assert seen == [2, 1, 1], "batch first, then one request per item"


def test_embedder_truncates_a_single_item_that_is_still_rejected() -> None:
    lengths: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        text = json.loads(request.content)["input"][0]
        lengths.append(len(text))
        if len(text) > 1500:
            return httpx.Response(413, json={"error": "too long"})
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0, 0.0, 0.0]}]})

    out = _embedder(handler).embed(["x" * 5000])

    assert out == [[1.0, 0.0, 0.0]]
    assert lengths == [5000, 5000, 1500], "full, per-item retry, then truncated"


def test_embedder_raises_when_even_the_truncated_retry_is_rejected() -> None:
    with pytest.raises(RuntimeError, match="even after truncation"):
        _embedder(lambda r: httpx.Response(400)).embed(["a"])


def test_embedder_raises_on_a_5xx() -> None:
    with pytest.raises(RuntimeError, match="HTTP 502"):
        _embedder(lambda r: httpx.Response(502)).embed(["a"])


def test_embedder_raises_on_a_network_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    with pytest.raises(RuntimeError, match="ReadTimeout"):
        _embedder(handler).embed(["a"])


def test_embedder_raises_on_a_vector_count_mismatch() -> None:
    handler = lambda r: httpx.Response(  # noqa: E731
        200, json={"data": [{"index": 0, "embedding": [1.0, 0.0, 0.0]}]}
    )
    with pytest.raises(RuntimeError, match="malformed"):
        _embedder(handler).embed(["a", "b"])


def test_embedder_raises_on_a_width_that_disagrees_with_the_config() -> None:
    handler = lambda r: httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0]}]})  # noqa: E731
    with pytest.raises(RuntimeError, match="EMBED_DIMENSIONS"):
        _embedder(handler).embed(["a"])


def test_embedder_raises_when_a_row_has_no_embedding_list() -> None:
    handler = lambda r: httpx.Response(200, json={"data": [{"index": 0, "embedding": "nope"}]})  # noqa: E731
    with pytest.raises(RuntimeError, match="embedding"):
        _embedder(handler).embed(["a"])


def test_embedder_raises_on_non_json() -> None:
    with pytest.raises(RuntimeError, match="non-JSON"):
        _embedder(lambda r: httpx.Response(200, text="oops")).embed(["a"])


def test_embedder_exposes_its_dimensions_and_closes_cleanly() -> None:
    emb = _embedder(dimensions=768)
    assert emb.dimensions == 768
    assert emb._client() is emb._client()
    emb.close()
    assert emb._http is None


def test_embedder_from_settings_reuses_the_llm_timeout() -> None:
    settings = Settings(
        _env_file=None,
        embed_base_url="http://host/v1",
        embed_api_key="k",
        embed_model="nomic-embed-text",
        embed_dimensions=768,
        llm_timeout=99,
    )
    emb = OpenAIEmbedder.from_settings(settings)
    assert emb.dimensions == 768
    assert emb._timeout == 99


# ======================================================================= #
# JiraCloudTracker
# ======================================================================= #
def _jira_settings(**over) -> Settings:
    base = {
        "jira_base_url": "https://acme.atlassian.net/",
        "jira_email": "a@b.c",
        "jira_api_token": "t",
        "jira_project_key": "SUP",
    }
    base.update(over)
    return Settings(_env_file=None, **base)


@pytest.mark.parametrize(
    "blank", ["jira_base_url", "jira_email", "jira_api_token", "jira_project_key"]
)
def test_jira_fails_loudly_on_missing_config(blank) -> None:
    with pytest.raises(ValueError, match=blank):
        JiraCloudTracker(_jira_settings(**{blank: ""}))


def test_jira_creates_an_issue_and_returns_its_browse_url(monkeypatch) -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(201, json={"key": "SUP-42"})

    _patch_client(monkeypatch, handler)
    url = JiraCloudTracker(_jira_settings()).create_issue("t", "line one\n\nline two")

    assert url == "https://acme.atlassian.net/browse/SUP-42"
    assert captured["url"].endswith("/rest/api/3/issue")
    assert captured["body"]["fields"]["project"]["key"] == "SUP"
    assert captured["body"]["fields"]["issuetype"]["name"] == "Task"


def test_jira_raises_on_a_rejected_create(monkeypatch) -> None:
    _patch_client(monkeypatch, lambda r: httpx.Response(403, text="denied"))
    with pytest.raises(RuntimeError, match="403"):
        JiraCloudTracker(_jira_settings()).create_issue("t", "b")


def test_jira_raises_when_the_response_carries_no_issue_key(monkeypatch) -> None:
    _patch_client(monkeypatch, lambda r: httpx.Response(201, json={}))
    with pytest.raises(RuntimeError, match="no issue key"):
        JiraCloudTracker(_jira_settings()).create_issue("t", "b")


def test_jira_summary_is_single_line_and_capped() -> None:
    trunc = JiraCloudTracker._truncate_summary
    assert trunc("a\nb\tc") == "a b c"
    assert trunc("") == "KAI escalation"
    long = trunc("x" * 400)
    assert len(long) == 255 and long.endswith("...")


def test_jira_adf_splits_paragraphs_and_never_emits_an_empty_document() -> None:
    doc = JiraCloudTracker._to_adf("one\r\n\r\ntwo")
    assert [p["content"][0]["text"] for p in doc["content"]] == ["one", "two"]
    assert JiraCloudTracker._to_adf("")["content"][0]["content"][0]["text"] == " "


# ======================================================================= #
# ConfluenceCloudKBSource
# ======================================================================= #
def _conf_settings(**over) -> Settings:
    base = {
        "confluence_base_url": "https://acme.atlassian.net/wiki",
        "confluence_space_key": "ENG",
    }
    base.update(over)
    return Settings(_env_file=None, **base)


def _page(pid: str, title: str = "Page", body: str = "<p>text</p>") -> dict:
    return {
        "id": pid,
        "title": title,
        "body": {"storage": {"value": body}},
        "space": {"key": "ENG"},
        "metadata": {"labels": {"results": [{"name": "howto"}]}},
        "version": {"when": "2026-01-01T00:00:00Z"},
        "_links": {"webui": f"/spaces/ENG/pages/{pid}", "base": "https://acme.atlassian.net/wiki"},
    }


@pytest.mark.parametrize("blank", ["confluence_base_url", "confluence_space_key"])
def test_confluence_fails_loudly_on_missing_config(blank) -> None:
    with pytest.raises(ValueError, match=blank):
        ConfluenceCloudKBSource(_conf_settings(**{blank: ""}))


def test_confluence_rejects_an_email_with_no_token() -> None:
    with pytest.raises(ValueError, match="half-configured"):
        ConfluenceCloudKBSource(_conf_settings(confluence_email="a@b.c"))


@pytest.mark.parametrize(
    ("over", "auth_type"),
    [
        ({}, type(None)),
        ({"confluence_email": "a@b.c", "confluence_api_token": "t"}, httpx.BasicAuth),
        ({"confluence_api_token": "pat"}, object),
    ],
)
def test_confluence_picks_the_auth_mode_from_what_is_configured(over, auth_type) -> None:
    src = ConfluenceCloudKBSource(_conf_settings(**over))
    assert isinstance(src._auth, auth_type)


def test_confluence_yields_docs_and_namespaces_ids_by_host(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [_page("1"), _page("2")], "_links": {}})

    _patch_client(monkeypatch, handler)
    docs = list(ConfluenceCloudKBSource(_conf_settings()).iter_pages())

    assert [d.id for d in docs] == ["acme.atlassian.net:1", "acme.atlassian.net:2"]
    assert docs[0].url == "https://acme.atlassian.net/wiki/spaces/ENG/pages/1"
    assert docs[0].labels == ["howto"]
    assert docs[0].updated == "2026-01-01T00:00:00Z"


def test_confluence_paginates_until_a_short_page(monkeypatch) -> None:
    pages = [
        {"results": [_page(str(i)) for i in range(100)], "_links": {"next": "/next"}},
        {"results": [_page("100")], "_links": {}},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=pages.pop(0))

    _patch_client(monkeypatch, handler)
    docs = list(ConfluenceCloudKBSource(_conf_settings()).iter_pages())

    assert len(docs) == 101
    assert pages == []


def test_confluence_honours_the_max_docs_cap(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"results": [_page(str(i)) for i in range(100)], "_links": {"next": "/n"}}
        )

    _patch_client(monkeypatch, handler)
    docs = list(ConfluenceCloudKBSource(_conf_settings(confluence_max_docs=5)).iter_pages())

    assert len(docs) == 5


def test_confluence_raises_on_a_failed_content_request(monkeypatch) -> None:
    _patch_client(monkeypatch, lambda r: httpx.Response(401, text="nope"))
    with pytest.raises(RuntimeError, match="status 401"):
        list(ConfluenceCloudKBSource(_conf_settings()).iter_pages())


def test_confluence_marks_a_body_less_page_seen_but_does_not_yield_it(monkeypatch) -> None:
    """A page the crawler lacks permission to read must not be pruned as deleted."""

    def handler(request: httpx.Request) -> httpx.Response:
        blind = _page("9")
        blind["body"]["storage"]["value"] = ""
        return httpx.Response(200, json={"results": [blind], "_links": {}})

    _patch_client(monkeypatch, handler)
    src = ConfluenceCloudKBSource(_conf_settings())
    docs = list(src.iter_pages())

    assert docs == []
    assert src.seen_ids == {"acme.atlassian.net:9"}


def test_confluence_crawls_a_subtree_when_a_root_page_is_set(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/content/77"):
            return httpx.Response(200, json=_page("77", title="Root"))
        return httpx.Response(200, json={"results": [_page("78")], "_links": {}})

    _patch_client(monkeypatch, handler)
    docs = list(ConfluenceCloudKBSource(_conf_settings(confluence_root_page="77")).iter_pages())

    assert [d.title for d in docs] == ["Root", "Page"]


def test_confluence_resolves_a_root_page_given_by_title(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("title") == "Runbook":
            return httpx.Response(200, json={"results": [{"id": "55"}]})
        if request.url.path.endswith("/content/55"):
            return httpx.Response(200, json=_page("55", title="Runbook"))
        return httpx.Response(200, json={"results": [], "_links": {}})

    _patch_client(monkeypatch, handler)
    docs = list(
        ConfluenceCloudKBSource(_conf_settings(confluence_root_page="Runbook")).iter_pages()
    )

    assert [d.title for d in docs] == ["Runbook"]


def test_confluence_raises_when_a_named_root_page_does_not_exist(monkeypatch) -> None:
    _patch_client(monkeypatch, lambda r: httpx.Response(200, json={"results": []}))
    src = ConfluenceCloudKBSource(_conf_settings(confluence_root_page="Missing"))
    with pytest.raises(RuntimeError, match="root page not found"):
        list(src.iter_pages())


def test_confluence_falls_back_to_a_page_id_url_without_webui(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        page = _page("3")
        page["_links"] = {}
        return httpx.Response(200, json={"results": [page], "_links": {}})

    _patch_client(monkeypatch, handler)
    docs = list(ConfluenceCloudKBSource(_conf_settings()).iter_pages())

    assert docs[0].url.endswith("/pages/viewpage.action?pageId=3")


def test_confluence_never_puts_inline_credentials_into_a_doc_id() -> None:
    src = ConfluenceCloudKBSource(
        _conf_settings(confluence_base_url="https://user:secret@acme.atlassian.net/wiki")
    )
    assert src._host == "acme.atlassian.net"
    assert "secret" not in src._host

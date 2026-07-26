"""Regression tests for the deep-audit fixes: numeric-fabrication guard,
cross-instance Confluence id namespacing, and prune never deleting curated answers."""

from __future__ import annotations

from kai.config import Settings
from kai.interfaces import Chunk, Doc, ScoredChunk
from kai.pipeline.ask import _fabricated_numbers, _finalize_citations
from kai.pipeline.ingest import ingest_from
from kai.providers.confluence_cloud import ConfluenceCloudKBSource


def _sc(text: str, title: str = "T") -> ScoredChunk:
    return ScoredChunk(
        chunk=Chunk(id="c", doc_id="d", title=title, url="", text=text, space="s", ordinal=0),
        score=1.0,
        vector_score=1.0,
    )


# ---- NF-1: numeric-fabrication guard ------------------------------------------
def test_computed_number_absent_from_sources_is_flagged():
    src = [_sc("A /8 network has many usable host addresses.")]
    assert _fabricated_numbers("There are 4,160,768 usable addresses [1].", src) == "4,160,768"


def test_grounded_number_is_not_flagged():
    src = [_sc("RFC 1918 reserves 16,777,216 addresses in 10.0.0.0/8.")]
    assert _fabricated_numbers("It provides 16,777,216 addresses.", src) is None


def test_comma_vs_no_comma_normalization():
    src = [_sc("The limit is 16384 partitions.")]  # no comma in source
    assert _fabricated_numbers("Up to 16,384 partitions are allowed.", src) is None


def test_small_numbers_and_octets_ignored():
    src = [_sc("Kafka uses brokers and topics.")]  # no numbers in source at all
    # ports, years, counts, IP octets, version parts are all < 5 digits, no comma
    assert _fabricated_numbers("Use port 8080 in 2024 with 3 brokers on 10.0.0.1.", src) is None


def test_citation_markers_are_not_treated_as_numbers():
    assert _fabricated_numbers("See the docs [1][2][3].", [_sc("Some text.")]) is None


# ---- MSC-2: cross-instance Confluence id namespacing --------------------------
def _conf(base: str) -> ConfluenceCloudKBSource:
    return ConfluenceCloudKBSource(
        Settings(_env_file=None, confluence_base_url=base, confluence_space_key="S")
    )


def test_confluence_host_derived_from_base_url():
    assert _conf("https://acme.atlassian.net/wiki")._host == "acme.atlassian.net"


def test_same_page_id_on_two_instances_does_not_collide():
    a = _conf("https://acme.atlassian.net/wiki")
    b = _conf("https://other.example.com/confluence")
    result = {
        "id": "12345",
        "title": "Page",
        "body": {"storage": {"value": "<p>hello world</p>"}},
        "space": {"key": "S"},
        "version": {"when": "2024-01-01"},
        "metadata": {"labels": {"results": []}},
        "_links": {"webui": "/pages/12345", "base": "https://acme.atlassian.net/wiki"},
    }
    da, db = a._to_doc(dict(result)), b._to_doc(dict(result))
    assert da.id == "acme.atlassian.net:12345"
    assert da.id != db.id  # same page id, different instance -> no overwrite


# ---- MSC-1: prune must never delete curated answers ---------------------------
class _Emb:
    dimensions = 4

    def embed(self, texts):
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]


class _Store:
    def __init__(self):
        self.rows = {}

    def ensure_schema(self, dims):
        pass

    def delete(self, doc_id):
        self.rows.pop(doc_id, None)

    def upsert(self, chunks, vectors):
        self.rows[chunks[0].doc_id] = len(chunks)

    def list_doc_ids(self):
        return list(self.rows)


class _KB:
    def iter_pages(self):
        return iter(
            [
                Doc(
                    id="page-current",
                    title="t",
                    url="",
                    html="a fresh page with enough words to chunk",
                    content_type="text",
                )
            ]
        )


def test_prune_keeps_curated_but_drops_stale():
    store = _Store()
    store.rows["kai-curated:7"] = 1  # human-curated answer (no crawl source)
    store.rows["page-removed"] = 1  # genuinely gone upstream
    ingest_from(_Emb(), store, _KB(), prune=True)
    assert "kai-curated:7" in store.rows  # MUST survive prune
    assert "page-removed" not in store.rows  # stale page reconciled away
    assert "page-current" in store.rows


class _ErroredKB:
    errors = 1  # a source failed mid-crawl

    def iter_pages(self):
        return iter(
            [
                Doc(
                    id="page-current",
                    title="t",
                    url="",
                    html="a fresh page with enough words to chunk",
                    content_type="text",
                )
            ]
        )


def test_prune_skipped_when_a_source_errored():
    store = _Store()
    store.rows["stale-page"] = 1
    store.rows["page-current"] = 1
    ingest_from(_Emb(), store, _ErroredKB(), prune=True)
    assert "stale-page" in store.rows  # prune skipped: a partial crawl isn't trusted
    assert "page-current" in store.rows


# ---- NF-SUBSTR-1 / NF-DECIMAL-2: numeric-fabrication guard edge cases ----------
def test_number_not_excused_by_substring_of_larger_source_number():
    assert _fabricated_numbers("Total is 12345.", [_sc("the id is 912345678")]) == "12345"


def test_fabricated_decimal_is_flagged():
    assert (
        _fabricated_numbers("It achieves 28.7 BLEU [1].", [_sc("The model scores 28.4 BLEU.")])
        == "28.7"
    )


def test_grounded_decimal_not_flagged():
    assert _fabricated_numbers("Dropout is 0.1.", [_sc("Dropout rate is 0.1 here.")]) is None


def test_ip_and_version_strings_not_flagged():
    assert _fabricated_numbers("Use 10.0.0.1 on v2.3.1.", [_sc("no numbers here at all")]) is None


def test_number_reformatting_not_flagged():
    # Same VALUE, different text, must NOT be treated as fabricated (was a false +).
    assert _fabricated_numbers("It allows 5000.0 connections.", [_sc("limit is 5,000")]) is None
    assert _fabricated_numbers("Scale to 1,000,000 rows.", [_sc("up to 1e6 rows")]) is None
    assert _fabricated_numbers("The rate is 0.50.", [_sc("rate of 0.5 applies")]) is None


def test_giant_exponent_does_not_crash():
    # 1e400 -> inf; canonicalization must not raise OverflowError (would 500 /ask).
    assert _fabricated_numbers("Throughput is 1e400 ops.", [_sc("no numbers here")]) == "1e400"
    assert _fabricated_numbers("It is 1e400.", [_sc("the value 1e400 appears")]) is None


def test_large_integer_fabrication_caught_exactly():
    # > 2^53: float() would collapse ...993 and ...992 to the same value; exact int
    # comparison must still flag the fabricated one.
    assert (
        _fabricated_numbers("Total 9007199254740993.", [_sc("count is 9007199254740992")])
        == "9007199254740993"
    )


def test_first_ungrounded_of_several_is_flagged():
    assert (
        _fabricated_numbers("From 16,384 to 99,999.", [_sc("The limit is 16384 partitions.")])
        == "99,999"
    )


# ---- NF-3: finalize drops phantom out-of-range citation markers ----------------
def test_finalize_drops_all_out_of_range_markers():
    import re

    text, cites = _finalize_citations(
        "The broker elects a leader [5] and also [9].", [_sc("kafka broker partition leader")]
    )
    assert not re.search(r"\[\d+\]", text)  # phantom markers stripped from the text
    assert len(cites) == 1  # still exactly one citation


# ---- ingest: empty-doc clears prior rows; incremental skip + url/space re-embed ----
class _EmptyKB:
    def iter_pages(self):
        return iter([Doc(id="p1", title="t", url="", html="   ", content_type="text")])


def test_empty_doc_clears_prior_rows():
    store = _Store()
    store.rows["p1"] = 3  # a page that is now empty
    total = ingest_from(_Emb(), store, _EmptyKB())
    assert "p1" not in store.rows  # delete-on-empty keeps the store an exact mirror
    assert total == 0


class _HashStore(_Store):
    def __init__(self):
        super().__init__()
        self.hashes = {}

    def doc_hashes(self):
        return dict(self.hashes)

    def set_doc_hash(self, doc_id, h):
        self.hashes[doc_id] = h


class _CountEmb(_Emb):
    def __init__(self):
        self.calls = 0

    def embed(self, texts):
        self.calls += 1
        return super().embed(texts)


class _OneDocKB:
    def __init__(self, url="", space=""):
        self.url, self.space = url, space

    def iter_pages(self):
        return iter(
            [
                Doc(
                    id="p1",
                    title="t",
                    url=self.url,
                    space=self.space,
                    html="a page with enough words here to form a chunk",
                    content_type="text",
                )
            ]
        )


def test_incremental_skips_unchanged_and_reembeds_on_url_or_space_change():
    store, emb = _HashStore(), _CountEmb()
    ingest_from(emb, store, _OneDocKB(url="u1", space="s1"))
    first = emb.calls
    assert first > 0
    ingest_from(emb, store, _OneDocKB(url="u1", space="s1"))  # identical -> skipped
    assert emb.calls == first
    ingest_from(emb, store, _OneDocKB(url="u2", space="s1"))  # url changed -> re-embed
    assert emb.calls > first
    mid = emb.calls
    ingest_from(emb, store, _OneDocKB(url="u2", space="s2"))  # space changed -> re-embed
    assert emb.calls > mid


# ---- round 2: numeric-guard false-positive / false-negative edges -------------
def test_sentence_comma_does_not_make_small_int_significant():
    # "3, 4" must NOT be flagged: a sentence comma isn't a thousands separator.
    src = [_sc("no numbers here at all")]
    assert _fabricated_numbers("There are 3, possibly 4, options available.", src) is None
    assert _fabricated_numbers("Run step 7, then restart.", src) is None


def test_source_comma_list_members_are_grounded():
    # a value cited from a comma-separated list in the source is grounded
    src = [_sc("valid sizes are 16384,32768,65536 per broker")]
    assert _fabricated_numbers("Use up to 65536 partitions.", src) is None


def test_scientific_notation_flagged_and_grounded():
    assert _fabricated_numbers("It needs 1e9 operations.", [_sc("no numbers")]) == "1e9"
    assert _fabricated_numbers("about 1e9 ops", [_sc("roughly 1e9 operations total")]) is None


# ---- round 2: confluence host strips credentials; _to_doc skip branches --------
def test_host_strips_userinfo_and_keeps_port():
    assert _conf("https://user:pass@acme.atlassian.net/wiki")._host == "acme.atlassian.net"
    assert _conf("https://user:pass@wiki.corp.com:8443/x")._host == "wiki.corp.com:8443"


def test_to_doc_skips_empty_body_and_missing_id():
    s = _conf("https://acme.atlassian.net/wiki")
    assert (
        s._to_doc(
            {"id": "9", "title": "X", "body": {"storage": {"value": ""}}, "space": {"key": "S"}}
        )
        is None
    )  # no body -> skipped
    assert s._to_doc({"id": "", "body": {"storage": {"value": "<p>hi</p>"}}}) is None  # no id

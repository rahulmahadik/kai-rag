"""Tests for the answer-vs-escalate guards — the 'never give wrong info' core.

Direct coverage of `_looks_like_idk` (the IDK detector) and `_confidence`.
"""

from kai.interfaces import Chunk, ScoredChunk
import re

from kai.pipeline.ask import (
    _answer_grounding,
    _confidence,
    _fabricated_specifics,
    _finalize_citations,
    _looks_like_idk,
    _strip_model_sources_block,
    _tidy_answer,
)


def _sc(text="x", *, vector_score=0.6, rerank_score=None, score=1.0):
    chunk = Chunk(
        id="d#0", doc_id="d", title="T", url="https://u/1", text=text, space="S", ordinal=0
    )
    return ScoredChunk(
        chunk=chunk, score=score, vector_score=vector_score, rerank_score=rerank_score
    )


# --- _looks_like_idk -------------------------------------------------------


def test_idk_exact_marker():
    assert _looks_like_idk("I don't know")


def test_idk_long_uncited_prose_refusal():
    # B1: a long, UNCITED prose hedge must be caught (was slipping through).
    assert _looks_like_idk(
        "Based on the provided context, the source does not contain any "
        "information about the VPN reset procedure, so I am not able to answer "
        "this question from the knowledge base provided."
    )


def test_idk_hedged_opener():
    assert _looks_like_idk("Unfortunately, I cannot answer that from the sources.")


def test_cited_answer_mentioning_phrase_is_not_idk():
    # A real answer that cites [1] and mentions a refusal phrase in passing passes.
    assert not _looks_like_idk(
        "Source [1] does not contain the deadline, but it states the review is weekly [1]."
    )


def test_normal_cited_answer_is_not_idk():
    assert not _looks_like_idk("The mentoring programme pairs mentees with mentors [1].")


def test_empty_reply_is_idk():
    assert _looks_like_idk("   ")


def test_weak_phrase_in_real_content_is_not_idk():
    # A substantive UNCITED answer using a weak phrase about the DOMAIN (not the
    # sources) must NOT be misread as a refusal — the anchor requirement spares it.
    assert not _looks_like_idk(
        "The controller could not find an active leader, so it triggers a new "
        "leader election for the partition."
    )
    assert not _looks_like_idk(
        "A broker cannot answer a fetch request once the request timeout elapses."
    )


def test_weak_phrase_about_sources_is_idk():
    # Same weak phrase, but anchored to the sources/context → a genuine hedge.
    assert _looks_like_idk("The provided context does not contain that detail.")


def test_strong_refusal_uncited_is_idk():
    assert _looks_like_idk("There is no information about that topic here.")


# --- _confidence -----------------------------------------------------------


def test_confidence_zero_without_chunks():
    assert _confidence("anything", []) == 0.0


def test_confidence_in_unit_range_cosine_only():
    c = _confidence("mentoring programme mentors", [_sc("mentoring programme mentors")])
    assert 0.0 < c <= 1.0


def test_confidence_blends_cross_encoder_score():
    # A high cross-encoder logit must yield higher confidence than a low one.
    hi = _confidence("mentoring", [_sc("mentoring", vector_score=0.5, rerank_score=8.0)])
    lo = _confidence("mentoring", [_sc("mentoring", vector_score=0.5, rerank_score=-8.0)])
    assert hi > lo


# --- _fabricated_specifics (deterministic grounding guard) -----------------


def test_fabricated_specifics_flags_invented_class_and_url():
    # Source only NAMES the tool; answer invents config not present -> flagged.
    src = [_sc("Kafka Connect is a framework for sources and sinks; connectors at Confluent Hub.")]
    fabricated = "Use org.postgresql.Driver with jdbc:postgresql://host:5432/db via the REST API."
    flagged = _fabricated_specifics(fabricated, src)
    assert "org.postgresql.Driver" in flagged
    assert any(t.startswith("jdbc:postgresql://") for t in flagged)


def test_grounded_specifics_not_flagged():
    # Specifics that DO appear in the source must not be flagged.
    src = [_sc("The kafka-ganglia reporter is hosted at https://github.com/criteo/kafka-ganglia")]
    answer = "The kafka-ganglia reporter is at https://github.com/criteo/kafka-ganglia [1]."
    assert _fabricated_specifics(answer, src) == []


def test_prose_and_in_source_config_key_not_flagged():
    src = [_sc("Per-topic config lives in server.properties and requires a restart.")]
    answer = "You edit server.properties, e.g. to change a setting [1]."
    assert _fabricated_specifics(answer, src) == []


def test_two_segment_idiom_and_http_domain_not_flagged():
    # Generic 2-segment method calls and http doc-link domains must not be flagged.
    src = [_sc("see the code that prints errors")]
    assert _fabricated_specifics("return e.getMessage();", src) == []
    assert _fabricated_specifics("visit https://made-up.example.com/x", src) == []


def test_fabricated_specifics_no_substring_leak_across_words():
    # The leak: a fabricated dotted key whose letters appear contiguously across
    # separate source words must STILL be flagged (source prose != the identifier).
    src = [_sc("The system uses a metadata cache to speed lookups.")]
    assert "meta.data.cache" in _fabricated_specifics("Set meta.data.cache=true [1].", src)


def test_grounded_dotted_identifier_present_in_source_not_flagged():
    # When the source genuinely contains the identifier, it must not be flagged.
    src = [_sc("Configure org.apache.kafka.connect via the worker properties.")]
    assert _fabricated_specifics("Use org.apache.kafka.connect here [1].", src) == []


def test_fabricated_prefix_of_real_identifier_is_flagged():
    # A fabricated PREFIX of a real source identifier must NOT be excused by a raw
    # substring match — it's a different (shorter) identifier.
    src = [_sc("The class is org.apache.kafka.connect.Worker in the runtime.")]
    assert "com.foo.bar" in _fabricated_specifics("See com.foo.bar for setup [1].", src)


# --- _answer_grounding (deterministic anti-fabrication) --------------------


def test_answer_grounding_high_when_drawn_from_sources():
    src = [_sc("The controller manages partition leadership and broker registration.")]
    answer = "The controller manages partition leadership and broker registration."
    assert _answer_grounding(answer, src) > 0.8


def test_answer_grounding_low_for_fabrication():
    src = [_sc("Kafka Connect is a framework for sources and sinks.")]
    fabricated = (
        "Configure the postgres jdbc driver class, set the polling interval, "
        "define table whitelist, batch size, and deploy via the connect rest endpoint."
    )
    assert _answer_grounding(fabricated, src) < 0.45


# --- _finalize_citations (markers must match the Sources list 1:1) ---------


def _scu(title, url):
    chunk = Chunk(id=url, doc_id=url, title=title, url=url, text="t", space="S", ordinal=0)
    return ScoredChunk(chunk=chunk, score=1.0, vector_score=0.6)


def test_finalize_collapses_same_page_markers_to_one():
    # 8 chunks of the SAME page -> markers [1]..[8] collapse to a single [1].
    scored = [_scu("Replication tools", "https://x/repl") for _ in range(8)]
    new_ans, cites = _finalize_citations("A [1]. B [2]. C [3][7][8].", scored)
    assert len(cites) == 1 and cites[0].url == "https://x/repl"
    assert set(re.findall(r"\[(\d+)\]", new_ans)) == {"1"}


def test_finalize_renumbers_multiple_sources_in_appearance_order():
    scored = [_scu("A", "https://x/a"), _scu("B", "https://x/b"), _scu("A2", "https://x/a")]
    new_ans, cites = _finalize_citations("p [1]. q [3]. r [2].", scored)
    assert [c.url for c in cites] == ["https://x/a", "https://x/b"]
    assert new_ans == "p [1]. q [1]. r [2]."


def test_finalize_drops_out_of_range_marker():
    scored = [_scu("A", "https://x/a")]
    new_ans, cites = _finalize_citations("fact [1] and also [5].", scored)
    assert len(cites) == 1
    assert set(re.findall(r"\[(\d+)\]", new_ans)) == {"1"}


def test_finalize_no_markers_falls_back_to_top_source():
    scored = [_scu("Top", "https://x/top"), _scu("Other", "https://x/o")]
    new_ans, cites = _finalize_citations("An answer with no citations.", scored)
    assert new_ans == "An answer with no citations."
    assert len(cites) == 1 and cites[0].url == "https://x/top"


def test_finalize_glued_in_range_marker_is_a_citation():
    # Models emit "Kafka[1]" with no space — an IN-RANGE glued marker is renumbered.
    scored = [_scu("A", "https://x/a")]
    out, cites = _finalize_citations("Replication in Kafka[1] is durable.", scored)
    assert "[1]" in out and len(cites) == 1


def test_finalize_glued_out_of_range_left_alone_not_corrupted():
    # A glued OUT-OF-RANGE bracket in unfenced prose is an array index, not a citation
    # — it must be left intact (never dropped, which would corrupt the text).
    scored = [_scu("A", "https://x/a")]
    out, _ = _finalize_citations("Use cfg[0] and items[5] then see [1].", scored)
    assert "cfg[0]" in out and "items[5]" in out and "[1]" in out


# --- _strip_model_sources_block (drop the model's redundant trailing Sources) ----


def test_strip_sources_heading_with_bare_markers():
    # The reported bug: a trailing "Sources:" list of bare (duplicated) markers.
    text = "Replication copies data [1].\n\nSources:\n[1]\n[2]\n[2]"
    assert _strip_model_sources_block(text) == "Replication copies data [1]."


def test_strip_trailing_bare_markers_without_heading():
    assert _strip_model_sources_block("Answer body [1].\n[1]\n[2]") == "Answer body [1]."


def test_strip_references_heading_with_titled_lines():
    text = "Body [1].\n\n**References:**\n- [1] Kafka Replication\n- [2] Design V2"
    assert _strip_model_sources_block(text) == "Body [1]."


def test_strip_leaves_inline_citations_untouched():
    body = "Replication copies data to followers [1], acked by the leader [2]."
    assert _strip_model_sources_block(body) == body


def test_strip_does_not_touch_sentence_starting_with_marker():
    # A trailing line that starts with a marker but is prose (not a bare ref, no
    # heading) must be left alone — never delete real content.
    body = "Setup is done.\n[1] is the most important step, so do it first."
    assert _strip_model_sources_block(body) == body


def test_strip_then_finalize_renumbers_only_inline():
    scored = [_scu("A", "https://x/a"), _scu("B", "https://x/b")]
    new_ans, cites = _finalize_citations("p [1]. q [2].\n\nSources:\n[1]\n[2]\n[2]", scored)
    assert new_ans == "p [1]. q [2]."  # trailing block gone
    assert [c.url for c in cites] == ["https://x/a", "https://x/b"]


# --- _tidy_answer (presentation cleanup) -----------------------------------


def test_tidy_strips_editorial_opener_and_capitalizes():
    out = _tidy_answer("Based on the provided context sources, here are the tools [1].")
    assert out == "Here are the tools [1]."


def test_tidy_collapses_double_spaces_and_space_before_punct():
    assert _tidy_answer("The  controller  manages it .") == "The controller manages it."


def test_tidy_preserves_newlines_and_normal_text():
    src = "Line one [1].\nLine two [2]."
    assert _tidy_answer(src) == src


def test_tidy_leaves_non_editorial_answer_unchanged():
    src = "The controller elects partition leaders [1]."
    assert _tidy_answer(src) == src


def test_tidy_fixes_space_before_punctuation_and_double_period():
    # Space/tab before punctuation is joined; NEWLINES are deliberately preserved
    # (folding them corrupted code blocks — see the code-aware tidy).
    assert (
        _tidy_answer("It replicates data . It is durable..") == "It replicates data. It is durable."
    )


def test_tidy_preserves_code():
    code = 'See:\n```python\ndef f():\n    return data.get("items", [])\n```\nUse `r.json()` and ports[0].'
    out = _tidy_answer(code)
    assert "def f():" in out and '("items", [])' in out
    assert "`r.json()`" in out and "ports[0]" in out


def test_tidy_keeps_ellipsis():
    assert _tidy_answer("Wait for it... done.") == "Wait for it... done."

"""Query normalization: strip conversational filler, keep the topic; never empty."""

from kai.pipeline.ask import _normalize_query as n


def _toks(s):
    return s.lower().split()


def test_strips_show_me_details_of():
    assert _toks(n("show me details of RFC 1918")) == ["rfc", "1918"]


def test_strips_tell_me_about():
    assert "tell" not in n("tell me about Kafka replication").lower()
    assert "replication" in n("tell me about Kafka replication").lower()


def test_strips_trailing_please():
    assert n("show me details of RFC 1918 please").lower().strip().endswith("1918")


def test_interrogative_untouched():
    assert n("what is RFC 1918") == "what is RFC 1918"
    assert n("how does replication work") == "how does replication work"


def test_never_reduces_to_empty():
    assert n("please") == "please"  # nothing substantive after -> original
    assert n("show me") == "show me"


def test_plain_topic_untouched():
    assert n("Performance testing") == "Performance testing"
    assert n("Kafka replication") == "Kafka replication"


def test_oos_topic_survives_stripping():
    # stripping the lead-in must NOT change the topic (so OOS stays OOS)
    assert "france" in n("tell me about the capital of France").lower()

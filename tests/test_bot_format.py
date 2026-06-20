"""Unit tests for the Webex bot's pure reply formatter (no SDK, no network)."""

from kai.bot import format_reply


def test_answer_with_citations_appends_sources():
    out = format_reply(
        {
            "answer": "The mentoring programme pairs mentees with mentors [1].",
            "citations": [{"title": "Apache Mentoring Programme", "url": "https://x/AMP"}],
            "escalated": False,
        }
    )
    assert "mentoring programme pairs" in out
    assert "**Sources:**" in out
    assert "[Apache Mentoring Programme](https://x/AMP)" in out


def test_escalated_answer_has_no_sources_block():
    out = format_reply(
        {
            "answer": "I couldn't answer this confidently, so I've raised a ticket: https://t/1",
            "citations": [],
            "escalated": True,
        }
    )
    assert "raised a ticket" in out
    assert "**Sources:**" not in out


def test_citations_deduped_by_url():
    out = format_reply(
        {
            "answer": "X [1][2].",
            "citations": [
                {"title": "A", "url": "https://x/1"},
                {"title": "A again", "url": "https://x/1"},
                {"title": "B", "url": "https://x/2"},
            ],
            "escalated": False,
        }
    )
    assert out.count("https://x/1") == 1
    assert "https://x/2" in out


def test_empty_answer_is_graceful():
    out = format_reply({"answer": "", "citations": [], "escalated": False})
    assert out == "I don't have an answer for that."

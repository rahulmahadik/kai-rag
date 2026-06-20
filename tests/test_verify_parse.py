"""The verifier verdict parser must read FAIL even when the model wraps it.

A plain startswith("FAIL") let a wrapped verdict (```FAIL, "FAIL", "Verdict: FAIL")
slip through as PASS, silently shipping a bad answer. The parser now scans letters
only: FAIL wins if present, else PASS, else fail-open (PASS) for genuine noise.
"""

from kai.pipeline.verify import _verdict_passes


def test_plain_fail_is_rejected():
    assert _verdict_passes("FAIL") is False


def test_plain_pass_is_accepted():
    assert _verdict_passes("PASS") is True


def test_wrapped_fail_is_rejected():
    assert _verdict_passes("```\nFAIL\n```") is False
    assert _verdict_passes('"FAIL"') is False
    assert _verdict_passes("Verdict: FAIL") is False
    assert _verdict_passes("  fail.  ") is False


def test_unparseable_fails_open_to_pass():
    # Genuine noise must not block an already-gated answer (verifier is additive).
    assert _verdict_passes("") is True
    assert _verdict_passes("42") is True


def test_chatty_pass_not_misread_as_fail():
    # "failures" contains the substring "fail" — token matching must not trip on it.
    assert _verdict_passes("PASS, no failures found") is True
    assert _verdict_passes("PASS — the answer is fully grounded") is True

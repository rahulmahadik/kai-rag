"""Chunker robustness: real-web HTML cleaning + markdown fenced-code handling."""

from kai.pipeline.chunk import _split_sections, html_to_text


def test_html_strips_script_style_and_comments():
    html = (
        "<html><head><title>t</title></head><body>"
        "<script>var x=1;alert('hi')</script>"
        "<p>Real content here</p>"
        "<style>.a{color:red}</style>"
        "<!-- a hidden comment -->"
        "</body></html>"
    )
    out = html_to_text(html)
    assert "Real content here" in out
    assert "alert" not in out and "var x" not in out
    assert "color:red" not in out
    assert "hidden comment" not in out


def test_confluence_html_still_chunks():
    # Regression: the script/style strip must not harm normal Confluence HTML.
    out = html_to_text("<h2>Replication</h2><p>Leaders and <b>followers</b>.</p>")
    assert "## Replication" in out
    assert "Leaders and followers" in out


def test_fenced_code_hash_line_not_a_heading():
    text = "## Real Heading\nbody text\n```\n# this is a code comment\nx = 1\n```\nmore"
    headings = [h for h, _ in _split_sections(text)]
    assert "Real Heading" in headings
    assert "this is a code comment" not in headings

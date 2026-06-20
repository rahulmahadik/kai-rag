"""Tables must survive HTML→text with cell boundaries intact.

Before the fix, <td>/<th> collapsed to a single space, so "9092" and "broker-1"
ran together and the value↔column mapping was lost for factual lookups. Cells are
now pipe-separated and rows stay on their own lines.
"""

from kai.pipeline.chunk import html_to_text


def test_table_cells_are_pipe_separated():
    html = (
        "<table>"
        "<tr><th>Broker</th><th>Host</th><th>Port</th></tr>"
        "<tr><td>1</td><td>a.example</td><td>9092</td></tr>"
        "<tr><td>2</td><td>b.example</td><td>9093</td></tr>"
        "</table>"
    )
    text = html_to_text(html)
    # Header row and each data row keep cell boundaries.
    assert "Broker | Host | Port" in text
    assert "1 | a.example | 9092" in text
    assert "2 | b.example | 9093" in text
    # Each row is on its own line (value not merged across rows).
    assert "9092" in text and "9093" in text
    assert "9092 | 2" not in text  # rows did not run together


def test_non_table_html_unaffected():
    assert html_to_text("<p>Hello <b>world</b></p>") == "Hello world"

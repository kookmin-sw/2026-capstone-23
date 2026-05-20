from eval.parsers.pipeline_engine import extract_pipeline_tables


def test_extract_pipeline_tables_prefers_table_markers_for_nested_tables():
    outer = (
        "<table>"
        "<tr><td>outer<table><tr><td>inner</td></tr></table></td></tr>"
        "</table>"
    )
    text = f"[[TABLE]]\n{outer}\n[[/TABLE]]"

    assert extract_pipeline_tables(text) == [outer]


def test_extract_pipeline_tables_falls_back_to_html_regex_without_markers():
    text = "<p>before</p><table><tr><td>cell</td></tr></table>"

    assert extract_pipeline_tables(text) == ["<table><tr><td>cell</td></tr></table>"]

from pathlib import Path

from eval.parsers.pipeline_engine import PipelineEngineAdapter


class FakePipeline:
    def __init__(self, output_path: Path):
        self.output_path = output_path

    def process_file(self, _file_path: Path) -> Path:
        return self.output_path


def test_pipeline_engine_pdf_text_fallback_escapes_html(tmp_path: Path):
    output_path = tmp_path / "result.txt"
    output_path.write_text("# source\n## Page 1\nA & B < C", encoding="utf-8")
    pdf_path = tmp_path / "input.pdf"
    pdf_path.write_bytes(b"not a real pdf")

    adapter = PipelineEngineAdapter.__new__(PipelineEngineAdapter)
    adapter.pipeline = FakePipeline(output_path)
    adapter.engine_name = "pipeline:test"

    parsed = adapter.parse(pdf_path)

    assert parsed.error is None
    assert parsed.text == "A & B < C"
    assert parsed.tables == ["<table><tr><td>A &amp; B &lt; C</td></tr></table>"]

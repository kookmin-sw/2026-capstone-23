import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.config import AppConfig
from core.conversion_common import ConversionError, run_command
from core.pipeline import DocumentPipeline


def test_process_excel_uses_combined_parser_for_xlsx(tmp_path: Path, monkeypatch):
    import core.converters as converters
    import core.spreadsheet_table_extractors as table_extractors

    sample = tmp_path / "book.xlsx"
    sample.write_bytes(b"stub")
    calls = {"combined": 0}

    def fake_combined(path: Path):
        calls["combined"] += 1
        assert path == sample
        return (
            {
                "page_count": 1,
                "pages": [
                    {
                        "page": 1,
                        "text": "",
                        "images": [],
                        "elements": [
                            {
                                "type": "table",
                                "content": "<table><tr><th>A</th></tr></table>",
                                "markdown": "| A |\n| --- |",
                                "bbox": None,
                            }
                        ],
                    }
                ],
            },
            [{"html": "<table><tr><td>A</td></tr></table>", "first_line": "A"}],
        )

    monkeypatch.setattr(table_extractors, "_parse_xlsx_combined", fake_combined)
    monkeypatch.setattr(
        converters,
        "parse_excel",
        lambda _path: pytest.fail("parse_excel should not run after combined xlsx parse"),
    )
    monkeypatch.setattr(
        converters,
        "parse_excel_tables",
        lambda _path: pytest.fail("parse_excel_tables should not run after combined xlsx parse"),
    )

    pipeline = DocumentPipeline.__new__(DocumentPipeline)
    pipeline.config = AppConfig(
        input_root=tmp_path,
        output_root=tmp_path / "outputs",
        tmp_root=tmp_path / "tmp",
        vlm_device="cpu",
    )

    output_path = pipeline._process_excel(sample)

    assert calls["combined"] == 1
    assert output_path.exists()
    meta = json.loads(output_path.with_suffix(".meta.json").read_text(encoding="utf-8"))
    assert meta["tables"] == [{"html": "<table><tr><td>A</td></tr></table>", "first_line": "A"}]


def test_run_command_raises_conversion_error_on_timeout(monkeypatch):
    def fake_run(*_args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=kwargs.get("args", ["fake"]), timeout=kwargs["timeout"])

    monkeypatch.setattr("core.conversion_common.subprocess.run", fake_run)

    with pytest.raises(ConversionError, match="timed out after 7 seconds"):
        run_command(["fake"], timeout=7)


def test_extract_pdf_skips_small_images_before_pixmap(tmp_path: Path, monkeypatch):
    import core.converters as converters
    import core.pdf_extractor as pdf_extractor

    class FakeRect:
        def __init__(self, x0: float, y0: float, x1: float, y1: float):
            self.x0 = x0
            self.y0 = y0
            self.x1 = x1
            self.y1 = y1
            self.width = x1 - x0
            self.height = y1 - y0

    class FakePage:
        rect = FakeRect(0, 0, 200, 200)

        def get_text(self, mode: str, **_kwargs):
            if mode == "dict":
                return {"blocks": []}
            return ""

        def get_images(self, full: bool = False):
            assert full is True
            return [(10,)]

        def get_image_bbox(self, _image):
            return FakeRect(0, 0, 10, 10)

        def get_drawings(self):
            return []

        def find_tables(self):
            return SimpleNamespace(tables=[])

    class FakeDoc:
        def __len__(self):
            return 1

        def load_page(self, index: int):
            assert index == 0
            return FakePage()

        def close(self):
            pass

    monkeypatch.setattr(pdf_extractor.fitz, "open", lambda _path: FakeDoc())
    monkeypatch.setattr(
        pdf_extractor.fitz,
        "Pixmap",
        lambda *_args, **_kwargs: pytest.fail("small images should skip Pixmap creation"),
    )
    monkeypatch.setattr(converters, "extract_table_regions_from_pdf_by_text", lambda *_args, **_kwargs: [])

    result = pdf_extractor.extract_pdf(tmp_path / "fake.pdf")

    assert result["page_count"] == 1
    assert result["pages"][0]["images"] == []

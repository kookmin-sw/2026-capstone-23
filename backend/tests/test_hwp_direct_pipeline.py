from pathlib import Path
from types import SimpleNamespace

import core.pipeline as pipeline_module
import core.qwen_staged_pipeline as qwen_staged_module
import core.converters as converters_module
from core.converters import ConversionError
from core.converters import parse_hwp_document_direct
from core.pipeline import DocumentPipeline
from core.qwen_staged_pipeline import preprocess_document_for_qwen


def _make_pipeline(tmp_path: Path) -> DocumentPipeline:
    pipeline = DocumentPipeline.__new__(DocumentPipeline)
    pipeline.config = SimpleNamespace(
        input_root=tmp_path,
        output_root=tmp_path / "outputs",
        tmp_root=tmp_path / "tmp",
        gpu_max_concurrent=1,
        vlm_max_concurrent=1,
    )
    pipeline.vlm = SimpleNamespace(
        use_qwen=False,
        use_deepseek=False,
        describe_image=lambda *_args, **_kwargs: {"text": "이미지 설명"},
    )
    return pipeline


def test_pipeline_processes_hwp_text_without_pdf_page_vlm(tmp_path, monkeypatch):
    source_path = tmp_path / "sample.hwp"
    source_path.write_bytes(b"hwp")
    pipeline = _make_pipeline(tmp_path)

    monkeypatch.setattr(
        pipeline_module,
        "parse_hwp_document_direct",
        lambda _path, **_kwargs: {
            "page_count": 1,
            "pages": [
                {
                    "page": 1,
                    "text": "직접 추출 본문",
                    "images": [],
                    "elements": [{"type": "text", "content": "직접 추출 본문", "bbox": [0, 0, 0, 0]}],
                }
            ],
            "images": [],
            "tables": [],
        },
    )

    def fail_pdf_fallback(*_args, **_kwargs):
        raise AssertionError("HWP direct parsing should avoid PDF/VLM page fallback")

    monkeypatch.setattr(DocumentPipeline, "_process_hwp_page_by_page", fail_pdf_fallback)

    output_path = pipeline.process_file(source_path)
    content = output_path.read_text(encoding="utf-8")

    assert "직접 추출 본문" in content
    assert "페이지 수: 1" in content


def test_parse_hwp_document_direct_uses_hwp5html_tables_first(tmp_path, monkeypatch):
    source_path = tmp_path / "html-first.hwp"
    source_path.write_bytes(b"hwp")
    html_root = tmp_path / "html-tmp"
    html_dir = html_root / "html-first_direct_html"

    monkeypatch.setattr(converters_module, "_is_hwpx_format", lambda _path, **_kwargs: False)

    def fake_run(_cmd, **_kwargs):
        html_dir.mkdir(parents=True, exist_ok=True)
        (html_dir / "index.xhtml").write_text(
            (
                "<html><body><div class='Page'>"
                "<p>문서 제목</p>"
                "<table><tr><td>항목</td><td>값</td></tr><tr><td>성명</td><td>홍길동</td></tr></table>"
                "</div></body></html>"
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr(converters_module.subprocess, "run", fake_run)

    def fail_hwp5txt(*_args, **_kwargs):
        raise AssertionError("hwp5txt fallback should not run when hwp5html succeeds")

    monkeypatch.setattr(converters_module, "extract_text_from_hwp_pyhwp", fail_hwp5txt)

    parsed = parse_hwp_document_direct(source_path, tmp_dir=html_root)

    assert parsed["source_type"] == "hwp_direct_html"
    assert parsed["pages"][0]["elements"][0]["content"] == "문서 제목"
    table = parsed["pages"][0]["elements"][1]
    assert table["type"] == "table"
    assert "<table" in table["content"]
    assert "| 항목 | 값 |" in table["markdown"]


def test_parse_hwp_document_direct_skips_hwp5txt_for_pyhwp_unsupported_ole(tmp_path, monkeypatch):
    source_path = tmp_path / "unsupported.hwp"
    source_path.write_bytes(b"hwp")

    monkeypatch.setattr(converters_module, "_is_hwpx_format", lambda _path, **_kwargs: False)

    def fake_run(_cmd, **_kwargs):
        return SimpleNamespace(
            returncode=1,
            stdout="",
            stderr=(
                "AttributeError: 'OleStream' object has no attribute "
                "'propertySetStream'"
            ),
        )

    monkeypatch.setattr(converters_module.subprocess, "run", fake_run)

    def fail_hwp5txt(*_args, **_kwargs):
        raise AssertionError("hwp5txt should not run for known pyhwp OLE parser failures")

    monkeypatch.setattr(converters_module, "extract_text_from_hwp_pyhwp", fail_hwp5txt)
    monkeypatch.setattr(
        converters_module,
        "_extract_text_from_hwp_bodytext",
        lambda _path: "BodyText 직접 파싱 본문",
    )

    parsed = parse_hwp_document_direct(source_path, tmp_dir=tmp_path / "html-tmp")

    assert parsed["pages"][0]["text"] == "BodyText 직접 파싱 본문"
    assert parsed["source_type"] == "hwp_direct_bodytext"


def test_parse_hwp_document_direct_preserves_embedded_image_page(tmp_path, monkeypatch):
    source_path = tmp_path / "multi.hwp"
    source_path.write_bytes(b"hwp")

    monkeypatch.setattr(converters_module, "_is_hwpx_format", lambda _path, **_kwargs: False)
    monkeypatch.setattr(
        converters_module,
        "extract_text_from_hwp_pyhwp",
        lambda _path, **_kwargs: "\n".join(f"line {idx}" for idx in range(90)),
    )
    monkeypatch.setattr(
        converters_module,
        "extract_images_from_hwp",
        lambda _path, **_kwargs: [
            {
                "image_id": "hwp-img-page-1",
                "page": 1,
                "bbox": [0, 0, 10, 10],
                "bytes": b"image-page-1",
            }
        ],
    )

    parsed = parse_hwp_document_direct(source_path, lines_per_page=80)

    assert parsed["page_count"] == 2
    assert parsed["pages"][0]["images"][0]["image_id"] == "hwp-img-page-1"
    assert parsed["pages"][0]["elements"][-1]["image_id"] == "hwp-img-page-1"
    assert parsed["pages"][1]["images"] == []


def test_pipeline_sends_only_hwp_embedded_images_to_vlm(tmp_path, monkeypatch):
    source_path = tmp_path / "image.hwp"
    source_path.write_bytes(b"hwp")
    calls = []
    pipeline = _make_pipeline(tmp_path)

    def describe_image(image_bytes, **kwargs):
        calls.append((image_bytes, kwargs))
        return {"text": "내장 이미지 분석"}

    pipeline.vlm.describe_image = describe_image
    monkeypatch.setattr(
        pipeline_module,
        "parse_hwp_document_direct",
        lambda _path, **_kwargs: {
            "page_count": 1,
            "pages": [
                {
                    "page": 1,
                    "text": "본문",
                    "images": [
                        {
                            "image_id": "hwp-img-1",
                            "page": 1,
                            "bbox": [0, 0, 10, 10],
                            "bytes": b"image-bytes",
                            "sha1": "sha1",
                        }
                    ],
                    "elements": [
                        {"type": "text", "content": "본문", "bbox": [0, 0, 0, 0]},
                        {"type": "image", "image_id": "hwp-img-1", "bbox": [0, 0, 10, 10]},
                    ],
                }
            ],
            "images": [],
            "tables": [],
        },
    )

    output_path = pipeline.process_file(source_path)
    content = output_path.read_text(encoding="utf-8")

    assert len(calls) == 1
    assert calls[0][0] == b"image-bytes"
    assert "내장 이미지 분석" in content


def test_pipeline_falls_back_to_existing_hwp_pdf_flow_when_direct_parse_fails(tmp_path, monkeypatch):
    source_path = tmp_path / "fallback.hwp"
    source_path.write_bytes(b"hwp")
    fallback_output = tmp_path / "outputs" / "fallback.txt"
    pipeline = _make_pipeline(tmp_path)

    monkeypatch.setattr(
        pipeline_module,
        "parse_hwp_document_direct",
        lambda _path, **_kwargs: (_ for _ in ()).throw(ConversionError("direct failed")),
    )

    def fake_fallback(_self, *_args, **_kwargs):
        fallback_output.parent.mkdir(parents=True, exist_ok=True)
        fallback_output.write_text("fallback", encoding="utf-8")
        return fallback_output

    monkeypatch.setattr(DocumentPipeline, "_process_hwp_page_by_page", fake_fallback)

    assert pipeline.process_file(source_path) == fallback_output
    assert fallback_output.read_text(encoding="utf-8") == "fallback"


def test_parse_hwp_document_direct_routes_legacy_hwp_to_pdf_fallback(tmp_path, monkeypatch):
    source_path = tmp_path / "legacy.hwp"
    source_path.write_bytes(b"legacy-hwp")

    monkeypatch.setattr(converters_module, "_is_hwpx_format", lambda _path, **_kwargs: False)
    monkeypatch.setattr(converters_module, "_is_legacy_hwp_format", lambda _path: True)
    monkeypatch.setattr(converters_module, "_hwp_version_label", lambda _path: "4.1.0.0")

    try:
        parse_hwp_document_direct(source_path, tmp_dir=tmp_path / "html-tmp")
    except ConversionError as exc:
        assert "PDF 변환 경로" in str(exc)
    else:
        raise AssertionError("legacy HWP should be routed to PDF fallback")


def test_convert_to_pdf_uses_legacy_converter_before_hwp5html(tmp_path, monkeypatch):
    source_path = tmp_path / "legacy.hwp"
    source_path.write_bytes(b"legacy-hwp")
    pdf_path = tmp_path / "out" / "legacy.pdf"

    monkeypatch.setattr(converters_module, "_is_legacy_hwp_format", lambda _path: True)
    monkeypatch.setattr(converters_module, "_hwp_version_label", lambda _path: "4.1.0.0")

    def fake_legacy_converter(_src, dst_dir):
        dst_dir.mkdir(parents=True, exist_ok=True)
        pdf_path.write_bytes(b"%PDF-legacy")
        return (pdf_path, {"simple_tables": [], "complex_tables": []})

    monkeypatch.setattr(converters_module, "convert_hwp_to_pdf_via_legacy_converter", fake_legacy_converter)

    def fail_hwp5html(*_args, **_kwargs):
        raise AssertionError("legacy HWP should not try hwp5html before legacy converter")

    monkeypatch.setattr(converters_module, "convert_hwp_to_pdf_via_html", fail_hwp5html)

    converted, table_info = converters_module.convert_to_pdf(source_path, tmp_path / "out")

    assert converted == pdf_path
    assert table_info == {"simple_tables": [], "complex_tables": []}


def test_qwen_preprocess_processes_hwp_without_pdf_when_direct_parse_succeeds(tmp_path, monkeypatch):
    source_path = tmp_path / "qwen.hwp"
    source_path.write_bytes(b"hwp")

    monkeypatch.setattr(
        qwen_staged_module,
        "parse_hwp_document_direct",
        lambda _path, **_kwargs: {
            "page_count": 1,
            "pages": [
                {
                    "page": 1,
                    "text": "큐웬 직접 추출",
                    "images": [],
                    "elements": [{"type": "text", "content": "큐웬 직접 추출", "bbox": [0, 0, 0, 0]}],
                }
            ],
            "tables": [],
        },
    )

    def fail_pdf_conversion(*_args, **_kwargs):
        raise AssertionError("Qwen HWP preprocess should avoid PDF conversion when direct parsing succeeds")

    monkeypatch.setattr(qwen_staged_module, "convert_to_pdf", fail_pdf_conversion)

    payload = preprocess_document_for_qwen(
        source_path=source_path,
        job_item_id="job-hwp",
        config=SimpleNamespace(tmp_root=tmp_path / "tmp"),
    )

    assert payload["sourceType"] == "hwp_direct_pyhwp"
    assert payload["pdfPath"] == ""
    assert payload["pages"][0]["elements"][0]["content"] == "큐웬 직접 추출"
    assert payload["inferenceInputs"] == []

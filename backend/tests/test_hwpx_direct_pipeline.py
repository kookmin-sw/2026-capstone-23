import base64
import zipfile
from types import SimpleNamespace

from core.pipeline import DocumentPipeline
from core.qwen_staged_pipeline import preprocess_document_for_qwen


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)


def _write_hwpx(path):
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "Contents/section0.xml",
            (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<hs:sec xmlns:hs="http://www.hancom.co.kr/hwpml/2011/section" '
                'xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph">'
                '<hp:p><hp:run><hp:t>요약 문단</hp:t></hp:run></hp:p>'
                '<hp:p><hp:tbl>'
                '<hp:tr>'
                '<hp:tc><hp:subList><hp:p><hp:run><hp:t>항목</hp:t></hp:run></hp:p></hp:subList></hp:tc>'
                '<hp:tc><hp:subList><hp:p><hp:run><hp:t>수치</hp:t></hp:run></hp:p></hp:subList></hp:tc>'
                "</hp:tr>"
                "</hp:tbl></hp:p>"
                "</hs:sec>"
            ),
        )


def test_pipeline_processes_hwpx_without_hwp_pdf_or_vlm_path(tmp_path, monkeypatch):
    source_path = tmp_path / "sample.hwpx"
    _write_hwpx(source_path)

    pipeline = DocumentPipeline.__new__(DocumentPipeline)
    pipeline.config = SimpleNamespace(
        input_root=tmp_path,
        output_root=tmp_path / "outputs",
        tmp_root=tmp_path / "tmp",
    )

    def fail_hwp_path(*_args, **_kwargs):
        raise AssertionError("HWP/PDF/VLM page pipeline should not run for HWPX")

    monkeypatch.setattr(DocumentPipeline, "_process_hwp_page_by_page", fail_hwp_path)

    output_path = pipeline.process_file(source_path)
    content = output_path.read_text(encoding="utf-8")

    assert "[[TABLE]]" in content
    assert "<td><p>항목</p></td>" in content
    assert "[[TABLE_MARKDOWN]]" in content
    assert "요약 문단" in content


def test_qwen_preprocess_sends_only_embedded_hwpx_images_to_vlm(tmp_path):
    source_path = tmp_path / "sample.hwpx"
    with zipfile.ZipFile(source_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "Contents/content.hpf",
            (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<opf:package xmlns:opf="http://www.idpf.org/2007/opf">'
                "<opf:manifest>"
                '<opf:item id="image1" href="BinData/image1.png" media-type="image/png" isEmbeded="1"/>'
                "</opf:manifest>"
                "</opf:package>"
            ),
        )
        archive.writestr(
            "Contents/section0.xml",
            (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<hs:sec xmlns:hs="http://www.hancom.co.kr/hwpml/2011/section" '
                'xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph" '
                'xmlns:hc="http://www.hancom.co.kr/hwpml/2011/core">'
                '<hp:p><hp:run><hp:t>본문</hp:t></hp:run></hp:p>'
                '<hp:p><hp:pic><hc:img binaryItemIDRef="image1"/></hp:pic></hp:p>'
                "</hs:sec>"
            ),
        )
        archive.writestr("BinData/image1.png", PNG_1X1)

    payload = preprocess_document_for_qwen(
        source_path=source_path,
        job_item_id="job-1",
        config=SimpleNamespace(tmp_root=tmp_path / "tmp"),
    )

    assert payload["sourceType"] == "hwpx_direct_xml"
    assert len(payload["inferenceInputs"]) == 1
    assert payload["inferenceInputs"][0]["imageId"] == "hwpx-img-1"
    assert payload["pages"][0]["elements"][1]["type"] == "image"

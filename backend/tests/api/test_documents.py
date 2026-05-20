import importlib
import io
import os
import sys
import types
import zipfile
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault("OPENAI_API_KEY", "test-key")

from core.converters import ConversionError
from infra.storage.file_assets import store_binary_asset
from infra.store import DOCUMENTS
from storage.sqlite_files import clear_file_blobs


def make_client(tmp_path: Path) -> TestClient:
    deps_module = types.ModuleType("api.dependencies")
    deps_module.config = SimpleNamespace(
        input_root=tmp_path / "inputs",
        output_root=tmp_path / "outputs",
        tmp_root=tmp_path / "tmp",
    )
    deps_module.pipeline = SimpleNamespace(
        update_vlm_model=lambda *_args, **_kwargs: None,
        process_file=lambda *_args, **_kwargs: None,
    )

    original_dependencies = sys.modules.get("api.dependencies")
    try:
        sys.modules["api.dependencies"] = deps_module
        import api.routers.parser as parser_module
        import api.routers.documents as documents_module

        parser_module = importlib.reload(parser_module)
        documents_module = importlib.reload(documents_module)
    finally:
        if original_dependencies is None:
            sys.modules.pop("api.dependencies", None)
        else:
            sys.modules["api.dependencies"] = original_dependencies

    app = FastAPI()
    app.include_router(documents_module.router, prefix="/api/v1")
    return TestClient(app)


def setup_function():
    DOCUMENTS.clear()
    clear_file_blobs()


def make_document_record(index: int) -> dict[str, object]:
    document_id = f"d_{index:03d}"
    return {
        "documentId": document_id,
        "title": f"sample-{index}",
        "sourceFilename": f"sample-{index}.png",
        "sourceFileType": "png",
        "originalFilename": f"sample-{index}.png",
        "originalFilePath": "",
        "fileType": "png",
        "uploadedAt": f"2026-03-25T00:00:{index:02d}Z",
        "updatedAt": f"2026-03-25T00:00:{index:02d}Z",
        "latestStatus": "COMPLETED",
        "jobId": f"j_{index:03d}",
        "error": None,
    }


def test_document_list_returns_all_items_by_default(tmp_path: Path):
    client = make_client(tmp_path)

    for index in range(55):
        record = make_document_record(index)
        DOCUMENTS[str(record["documentId"])] = record

    response = client.get("/api/v1/documents")
    assert response.status_code == 200

    payload = response.json()
    assert payload["success"] is True
    assert payload["error"] is None
    data = payload["data"]
    assert data["nextCursor"] is None
    assert len(data["items"]) == 55
    assert {item["documentId"] for item in data["items"]} == {
        f"d_{index:03d}" for index in range(55)
    }


def test_document_list_honors_explicit_limit(tmp_path: Path):
    client = make_client(tmp_path)

    for index in range(55):
        record = make_document_record(index)
        DOCUMENTS[str(record["documentId"])] = record

    response = client.get("/api/v1/documents?limit=10")
    assert response.status_code == 200

    items = response.json()["data"]["items"]
    assert [item["documentId"] for item in items] == [
        f"d_{index:03d}" for index in range(54, 44, -1)
    ]


def build_docx_bytes(*paragraphs: str) -> bytes:
    buffer = io.BytesIO()
    xml_body = "".join(f"<w:p><w:r><w:t>{paragraph}</w:t></w:r></w:p>" for paragraph in paragraphs)
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "word/document.xml",
            (
                "<?xml version='1.0' encoding='UTF-8'?>"
                "<w:document xmlns:w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'>"
                f"<w:body>{xml_body}</w:body>"
                "</w:document>"
            ),
        )
    return buffer.getvalue()


def build_hwpx_bytes(*paragraphs: str) -> bytes:
    buffer = io.BytesIO()
    paragraph_xml = "".join(
        f"<hp:p><hp:run><hp:t>{paragraph}</hp:t></hp:run></hp:p>" for paragraph in paragraphs
    )
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "Contents/section0.xml",
            (
                "<?xml version='1.0' encoding='UTF-8'?>"
                "<hp:sec xmlns:hp='http://www.hancom.co.kr/hwpml/2011/paragraph'>"
                f"{paragraph_xml}"
                "</hp:sec>"
            ),
        )
        archive.writestr("Contents/header.xml", "<hh:head xmlns:hh='http://www.hancom.co.kr/hwpml/2011/head' />")
    return buffer.getvalue()


def build_hwpx_table_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "Contents/section0.xml",
            (
                "<?xml version='1.0' encoding='UTF-8'?>"
                "<hp:sec xmlns:hp='http://www.hancom.co.kr/hwpml/2011/paragraph'>"
                "<hp:p><hp:run><hp:t>표 앞 문단</hp:t></hp:run></hp:p>"
                "<hp:p><hp:tbl>"
                "<hp:tr>"
                "<hp:tc><hp:subList><hp:p><hp:run><hp:t>항목</hp:t></hp:run></hp:p></hp:subList></hp:tc>"
                "<hp:tc><hp:subList><hp:p><hp:run><hp:t>값</hp:t></hp:run></hp:p></hp:subList></hp:tc>"
                "</hp:tr>"
                "</hp:tbl></hp:p>"
                "<hp:p><hp:run><hp:t>표 뒤 문단</hp:t></hp:run></hp:p>"
                "</hp:sec>"
            ),
        )
        archive.writestr("Contents/header.xml", "<hh:head xmlns:hh='http://www.hancom.co.kr/hwpml/2011/head' />")
    return buffer.getvalue()


def test_document_delete_removes_artifacts(tmp_path: Path):
    client = make_client(tmp_path)

    input_path = tmp_path / "inputs" / "incoming" / "sample.pdf"
    input_path.parent.mkdir(parents=True, exist_ok=True)
    input_path.write_bytes(b"%PDF-1.7")

    DOCUMENTS["d_1"] = {
        "documentId": "d_1",
        "title": "sample",
        "originalFilename": "sample.pdf",
        "originalFilePath": str(input_path),
        "fileType": "pdf",
        "uploadedAt": "2026-03-25T00:00:00Z",
        "latestStatus": "COMPLETED",
        "jobId": "j_1",
        "error": None,
    }

    delete_res = client.delete("/api/v1/documents/d_1")
    assert delete_res.status_code == 200

    deleted = delete_res.json()["data"]
    assert deleted["ok"] is True
    assert deleted["documentId"] == "d_1"
    assert deleted["fileExisted"] is True

    assert "d_1" not in DOCUMENTS
    assert not input_path.exists()
    assert not input_path.parent.exists()

    result_res = client.get("/api/v1/documents/d_1/result")
    assert result_res.status_code == 404
    assert result_res.json()["detail"]["error"]["code"] == "NOT_FOUND"


def test_document_delete_succeeds_even_if_artifacts_are_already_missing(tmp_path: Path):
    client = make_client(tmp_path)

    input_path = tmp_path / "inputs" / "missing.pdf"

    DOCUMENTS["d_missing"] = {
        "documentId": "d_missing",
        "title": "missing",
        "originalFilename": "missing.pdf",
        "originalFilePath": str(input_path),
        "fileType": "pdf",
        "uploadedAt": "2026-03-25T00:00:00Z",
        "latestStatus": "FAILED",
        "jobId": "j_missing",
        "error": {"code": "CONVERSION_FAIL", "message": "boom"},
    }

    delete_res = client.delete("/api/v1/documents/d_missing")
    assert delete_res.status_code == 200

    deleted = delete_res.json()["data"]
    assert deleted["ok"] is True
    assert deleted["fileExisted"] is False
    assert "d_missing" not in DOCUMENTS


def test_document_result_alias_returns_full_text_without_truncation(tmp_path: Path):
    client = make_client(tmp_path)

    output_path = tmp_path / "outputs" / "result.txt"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    full_text = "X" * 3000 + "\n" + "Y" * 3000
    output_path.write_text(full_text, encoding="utf-8")
    output_path.with_suffix(".meta.json").write_text('{"pageCount": 2}', encoding="utf-8")

    DOCUMENTS["d_result"] = {
        "documentId": "d_result",
        "title": "result",
        "originalFilename": "result.pdf",
        "originalFilePath": str(tmp_path / "inputs" / "result.pdf"),
        "fileType": "pdf",
        "uploadedAt": "2026-03-25T00:00:00Z",
        "updatedAt": "2026-03-25T00:00:00Z",
        "latestStatus": "COMPLETED",
        "jobId": "j_result",
        "jobItemId": "ji_result",
        "processingTimeMs": 10,
        "modelCode": "qwen2.5-vl-7b",
        "outputPath": str(output_path),
        "error": None,
    }

    result_res = client.get("/api/v1/documents/d_result/result")
    assert result_res.status_code == 200

    data = result_res.json()["data"]
    assert "original" not in data
    assert "originalUrl" not in data
    assert "downloadUrl" not in data
    assert data["txt"]["path"] == str(output_path)
    assert data["txt"]["preview"] == full_text
    assert len(data["txt"]["preview"]) > 5000
    assert data["meta"]["pageCount"] == 2


def test_document_result_alias_returns_result_only_for_docx(tmp_path: Path):
    client = make_client(tmp_path)

    output_path = tmp_path / "outputs" / "result-docx.txt"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("result", encoding="utf-8")

    DOCUMENTS["d_result_docx"] = {
        "documentId": "d_result_docx",
        "title": "result",
        "sourceFilename": "result.docx",
        "sourceFileType": "docx",
        "sourceFilePath": str(tmp_path / "inputs" / "result.docx"),
        "originalFilename": "result.docx",
        "originalFilePath": str(tmp_path / "inputs" / "result.docx"),
        "fileType": "docx",
        "uploadedAt": "2026-03-25T00:00:00Z",
        "updatedAt": "2026-03-25T00:00:00Z",
        "latestStatus": "COMPLETED",
        "jobId": "j_result_docx",
        "jobItemId": "ji_result_docx",
        "processingTimeMs": 10,
        "modelCode": "qwen2.5-vl-7b",
        "outputPath": str(output_path),
        "error": None,
    }

    result_res = client.get("/api/v1/documents/d_result_docx/result")
    assert result_res.status_code == 200

    data = result_res.json()["data"]
    assert "original" not in data
    assert data["txt"]["path"] == str(output_path)
    assert data["txt"]["preview"] == "result"


def test_document_download_returns_original_file(tmp_path: Path):
    client = make_client(tmp_path)

    input_path = tmp_path / "inputs" / "incoming" / "sample.hwp"
    input_path.parent.mkdir(parents=True, exist_ok=True)
    content = b"original-binary-content"
    input_path.write_bytes(content)

    DOCUMENTS["d_download"] = {
        "documentId": "d_download",
        "title": "sample",
        "originalFilename": "sample.hwp",
        "originalFilePath": str(input_path),
        "fileType": "hwp",
        "uploadedAt": "2026-03-25T00:00:00Z",
        "latestStatus": "QUEUED",
        "jobId": "j_download",
        "error": None,
    }

    response = client.get("/api/v1/documents/d_download/download")
    assert response.status_code == 200
    assert response.content == content
    assert "sample.hwp" in response.headers["content-disposition"]


def test_document_download_returns_hwpx_source_when_pdf_preview_asset_exists(tmp_path: Path):
    client = make_client(tmp_path)

    hwpx_content = build_hwpx_bytes("다운로드 원본")
    source_asset = store_binary_asset(
        category="document_source",
        filename="sample.hwpx",
        content=hwpx_content,
        document_id="d_download_hwpx_pdf_preview",
    )
    pdf_asset = store_binary_asset(
        category="document_original",
        filename="sample.pdf",
        content=b"%PDF-1.7 preview",
        document_id="d_download_hwpx_pdf_preview",
        media_type="application/pdf",
    )

    DOCUMENTS["d_download_hwpx_pdf_preview"] = {
        "documentId": "d_download_hwpx_pdf_preview",
        "title": "sample",
        "sourceFilename": "sample.hwpx",
        "sourceFileType": "hwpx",
        "sourceFileId": source_asset["fileId"],
        "sourceFilePath": source_asset["storagePath"],
        "originalFilename": "sample.pdf",
        "originalFileId": pdf_asset["fileId"],
        "originalFilePath": pdf_asset["storagePath"],
        "originalStorageKind": "converted_pdf",
        "fileType": "pdf",
        "uploadedAt": "2026-03-25T00:00:00Z",
        "latestStatus": "COMPLETED",
        "jobId": "j_download_hwpx_pdf_preview",
        "error": None,
    }

    response = client.get("/api/v1/documents/d_download_hwpx_pdf_preview/download")
    assert response.status_code == 200
    assert response.content == hwpx_content
    assert "sample.hwpx" in response.headers["content-disposition"]
    assert not response.headers["content-type"].startswith("application/pdf")


def test_document_original_returns_inline_image(tmp_path: Path):
    client = make_client(tmp_path)

    input_path = tmp_path / "inputs" / "incoming" / "sample.png"
    input_path.parent.mkdir(parents=True, exist_ok=True)
    content = b"\x89PNG\r\n\x1a\npreview"
    input_path.write_bytes(content)

    DOCUMENTS["d_preview"] = {
        "documentId": "d_preview",
        "title": "sample",
        "originalFilename": "sample.png",
        "originalFilePath": str(input_path),
        "fileType": "png",
        "uploadedAt": "2026-03-25T00:00:00Z",
        "latestStatus": "COMPLETED",
        "jobId": "j_preview",
        "error": None,
    }

    response = client.get("/api/v1/documents/d_preview/original")
    assert response.status_code == 200
    assert response.content == content
    assert response.headers["content-type"].startswith("image/png")
    assert response.headers["content-disposition"].startswith("inline;")


def test_document_original_converts_docx_to_inline_pdf(tmp_path: Path, monkeypatch):
    client = make_client(tmp_path)

    source_asset = store_binary_asset(
        category="document_source",
        filename="sample.docx",
        content=build_docx_bytes("First paragraph"),
        document_id="d_preview_docx",
    )

    def fake_convert_to_pdf(source_path: Path, dst_dir: Path):
        pdf_path = dst_dir / "sample.pdf"
        pdf_path.write_bytes(b"%PDF-1.7 docx-preview")
        return pdf_path

    import api.services.document_storage as document_storage_module

    monkeypatch.setattr(document_storage_module, "convert_to_pdf", fake_convert_to_pdf)

    DOCUMENTS["d_preview_docx"] = {
        "documentId": "d_preview_docx",
        "title": "sample",
        "sourceFilename": "sample.docx",
        "sourceFileType": "docx",
        "sourceFileId": source_asset["fileId"],
        "sourceFilePath": source_asset["storagePath"],
        "originalFilename": "sample.docx",
        "originalFileId": source_asset["fileId"],
        "originalFilePath": source_asset["storagePath"],
        "originalStorageKind": "source",
        "fileType": "docx",
        "uploadedAt": "2026-03-25T00:00:00Z",
        "latestStatus": "COMPLETED",
        "jobId": "j_preview_docx",
        "error": None,
    }

    response = client.get("/api/v1/documents/d_preview_docx/original")
    assert response.status_code == 200
    assert response.content == b"%PDF-1.7 docx-preview"
    assert response.headers["content-type"].startswith("application/pdf")
    assert response.headers["content-disposition"].startswith("inline;")

    updated_doc = DOCUMENTS["d_preview_docx"]
    assert updated_doc["originalFilename"] == "sample.pdf"
    assert updated_doc["fileType"] == "pdf"
    assert updated_doc["originalStorageKind"] == "converted_pdf"
    assert updated_doc["originalFileId"] != source_asset["fileId"]


def test_document_original_falls_back_to_html_for_docx_without_libreoffice(tmp_path: Path, monkeypatch):
    client = make_client(tmp_path)

    source_asset = store_binary_asset(
        category="document_source",
        filename="sample.docx",
        content=build_docx_bytes("First paragraph", "Second paragraph"),
        document_id="d_preview_docx_html",
    )

    def fake_convert_to_pdf(_source_path: Path, _dst_dir: Path):
        raise ConversionError("libreoffice 또는 unoconv가 필요합니다.")

    import api.services.document_storage as document_storage_module

    monkeypatch.setattr(document_storage_module, "convert_to_pdf", fake_convert_to_pdf)

    DOCUMENTS["d_preview_docx_html"] = {
        "documentId": "d_preview_docx_html",
        "title": "sample",
        "sourceFilename": "sample.docx",
        "sourceFileType": "docx",
        "sourceFileId": source_asset["fileId"],
        "sourceFilePath": source_asset["storagePath"],
        "originalFilename": "sample.docx",
        "originalFileId": source_asset["fileId"],
        "originalFilePath": source_asset["storagePath"],
        "originalStorageKind": "source",
        "fileType": "docx",
        "uploadedAt": "2026-03-25T00:00:00Z",
        "latestStatus": "COMPLETED",
        "jobId": "j_preview_docx_html",
        "error": None,
    }

    response = client.get("/api/v1/documents/d_preview_docx_html/original")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["content-disposition"].startswith("inline;")
    assert "<!doctype html>" in response.text.lower()
    assert "First paragraph" in response.text
    assert "Second paragraph" in response.text

    updated_doc = DOCUMENTS["d_preview_docx_html"]
    assert updated_doc["originalFilename"] == "sample.docx"
    assert updated_doc["fileType"] == "docx"
    assert updated_doc["originalStorageKind"] == "source"
    assert updated_doc["originalFileId"] == source_asset["fileId"]


def test_document_original_returns_html_for_docx_even_for_json_clients(tmp_path: Path, monkeypatch):
    client = make_client(tmp_path)

    source_asset = store_binary_asset(
        category="document_source",
        filename="sample.docx",
        content=build_docx_bytes("First paragraph"),
        document_id="d_preview_docx_json",
    )

    def fake_convert_to_pdf(_source_path: Path, _dst_dir: Path):
        raise ConversionError("libreoffice 또는 unoconv가 필요합니다.")

    import api.services.document_storage as document_storage_module

    monkeypatch.setattr(document_storage_module, "convert_to_pdf", fake_convert_to_pdf)

    DOCUMENTS["d_preview_docx_json"] = {
        "documentId": "d_preview_docx_json",
        "title": "sample",
        "sourceFilename": "sample.docx",
        "sourceFileType": "docx",
        "sourceFileId": source_asset["fileId"],
        "sourceFilePath": source_asset["storagePath"],
        "originalFilename": "sample.docx",
        "originalFileId": source_asset["fileId"],
        "originalFilePath": source_asset["storagePath"],
        "originalStorageKind": "source",
        "fileType": "docx",
        "uploadedAt": "2026-03-25T00:00:00Z",
        "latestStatus": "COMPLETED",
        "jobId": "j_preview_docx_json",
        "error": None,
    }

    response = client.get(
        "/api/v1/documents/d_preview_docx_json/original",
        headers={"accept": "application/json"},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "First paragraph" in response.text


def test_document_original_falls_back_to_html_for_hwpx_without_libreoffice(tmp_path: Path, monkeypatch):
    client = make_client(tmp_path)

    source_asset = store_binary_asset(
        category="document_source",
        filename="sample.hwpx",
        content=build_hwpx_bytes("첫 번째 문단", "두 번째 문단"),
        document_id="d_preview_hwpx_html",
    )

    import api.services.document_storage as document_storage_module

    monkeypatch.setattr(document_storage_module, "convert_hwp_to_pdf_via_libreoffice", lambda *_args: None)
    monkeypatch.setattr(document_storage_module, "render_hwpx_preview_pdf_bytes", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")))

    DOCUMENTS["d_preview_hwpx_html"] = {
        "documentId": "d_preview_hwpx_html",
        "title": "sample",
        "sourceFilename": "sample.hwpx",
        "sourceFileType": "hwpx",
        "sourceFileId": source_asset["fileId"],
        "sourceFilePath": source_asset["storagePath"],
        "originalFilename": "sample.hwpx",
        "originalFileId": source_asset["fileId"],
        "originalFilePath": source_asset["storagePath"],
        "originalStorageKind": "source",
        "fileType": "hwpx",
        "uploadedAt": "2026-03-25T00:00:00Z",
        "latestStatus": "COMPLETED",
        "jobId": "j_preview_hwpx_html",
        "error": None,
    }

    response = client.get("/api/v1/documents/d_preview_hwpx_html/original")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["content-disposition"].startswith("inline;")
    assert "첫 번째 문단" in response.text
    assert "두 번째 문단" in response.text

    updated_doc = DOCUMENTS["d_preview_hwpx_html"]
    assert updated_doc["originalFilename"] == "sample.hwpx"
    assert updated_doc["fileType"] == "hwpx"
    assert updated_doc["originalStorageKind"] == "source"
    assert updated_doc["originalFileId"] == source_asset["fileId"]


def test_document_original_uses_html_preview_for_hwpx_without_pdf_conversion(tmp_path: Path, monkeypatch):
    client = make_client(tmp_path)

    source_asset = store_binary_asset(
        category="document_source",
        filename="sample.hwpx",
        content=build_hwpx_table_bytes(),
        document_id="d_preview_hwpx_pdf",
    )

    import api.services.document_storage as document_storage_module

    def fail_pdf_conversion(*_args, **_kwargs):
        raise AssertionError("HWPX preview should not generate a PDF")

    monkeypatch.setattr(document_storage_module, "convert_hwp_to_pdf_via_libreoffice", fail_pdf_conversion)
    monkeypatch.setattr(document_storage_module, "render_hwpx_preview_pdf_bytes", fail_pdf_conversion)

    DOCUMENTS["d_preview_hwpx_pdf"] = {
        "documentId": "d_preview_hwpx_pdf",
        "title": "sample",
        "sourceFilename": "sample.hwpx",
        "sourceFileType": "hwpx",
        "sourceFileId": source_asset["fileId"],
        "sourceFilePath": source_asset["storagePath"],
        "originalFilename": "sample.hwpx",
        "originalFileId": source_asset["fileId"],
        "originalFilePath": source_asset["storagePath"],
        "originalStorageKind": "source",
        "fileType": "hwpx",
        "uploadedAt": "2026-03-25T00:00:00Z",
        "latestStatus": "COMPLETED",
        "jobId": "j_preview_hwpx_pdf",
        "error": None,
    }

    response = client.get("/api/v1/documents/d_preview_hwpx_pdf/original")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<table" in response.text
    assert "항목" in response.text

    updated_doc = DOCUMENTS["d_preview_hwpx_pdf"]
    assert updated_doc["originalFilename"] == "sample.hwpx"
    assert updated_doc["fileType"] == "hwpx"
    assert updated_doc["originalStorageKind"] == "source"
    assert updated_doc["originalFileId"] == source_asset["fileId"]


def test_document_original_uses_hwpx_html_when_pdf_preview_asset_exists(tmp_path: Path, monkeypatch):
    client = make_client(tmp_path)

    source_asset = store_binary_asset(
        category="document_source",
        filename="sample.hwpx",
        content=build_hwpx_table_bytes(),
        document_id="d_preview_hwpx_stale_pdf",
    )
    pdf_asset = store_binary_asset(
        category="document_original",
        filename="sample.pdf",
        content=b"%PDF-1.7 stale preview",
        document_id="d_preview_hwpx_stale_pdf",
        media_type="application/pdf",
    )

    import api.services.document_storage as document_storage_module

    def fail_pdf_conversion(*_args, **_kwargs):
        raise AssertionError("HWPX preview should not use a converted PDF asset")

    monkeypatch.setattr(document_storage_module, "convert_hwp_to_pdf_via_libreoffice", fail_pdf_conversion)
    monkeypatch.setattr(document_storage_module, "render_hwpx_preview_pdf_bytes", fail_pdf_conversion)

    DOCUMENTS["d_preview_hwpx_stale_pdf"] = {
        "documentId": "d_preview_hwpx_stale_pdf",
        "title": "sample",
        "sourceFilename": "sample.hwpx",
        "sourceFileType": "hwpx",
        "sourceFileId": source_asset["fileId"],
        "sourceFilePath": source_asset["storagePath"],
        "originalFilename": "sample.pdf",
        "originalFileId": pdf_asset["fileId"],
        "originalFilePath": pdf_asset["storagePath"],
        "originalStorageKind": "converted_pdf",
        "fileType": "pdf",
        "uploadedAt": "2026-03-25T00:00:00Z",
        "latestStatus": "COMPLETED",
        "jobId": "j_preview_hwpx_stale_pdf",
        "error": None,
    }

    response = client.get("/api/v1/documents/d_preview_hwpx_stale_pdf/original")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert b"%PDF" not in response.content
    assert "<table" in response.text
    assert "항목" in response.text


def test_document_original_keeps_pdf_preview_when_hwpx_source_asset_is_missing(tmp_path: Path):
    client = make_client(tmp_path)

    pdf_asset = store_binary_asset(
        category="document_original",
        filename="sample.pdf",
        content=b"%PDF-1.7 fallback preview",
        document_id="d_preview_hwpx_missing_source",
        media_type="application/pdf",
    )

    DOCUMENTS["d_preview_hwpx_missing_source"] = {
        "documentId": "d_preview_hwpx_missing_source",
        "title": "sample",
        "sourceFilename": "sample.hwpx",
        "sourceFileType": "hwpx",
        "sourceFileId": "sf_missing_hwpx_source",
        "sourceFilePath": "db/stored_files/sf_missing_hwpx_source/sample.hwpx",
        "originalFilename": "sample.pdf",
        "originalFileId": pdf_asset["fileId"],
        "originalFilePath": pdf_asset["storagePath"],
        "originalStorageKind": "converted_pdf",
        "fileType": "pdf",
        "uploadedAt": "2026-03-25T00:00:00Z",
        "latestStatus": "COMPLETED",
        "jobId": "j_preview_hwpx_missing_source",
        "error": None,
    }

    response = client.get("/api/v1/documents/d_preview_hwpx_missing_source/original")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/pdf")
    assert response.content == b"%PDF-1.7 fallback preview"


def test_document_original_does_not_fallback_to_pdf_for_hwpx_parse_errors(tmp_path: Path):
    client = make_client(tmp_path)

    source_asset = store_binary_asset(
        category="document_source",
        filename="broken.hwpx",
        content=b"not a zip",
        document_id="d_preview_hwpx_parse_error",
    )
    pdf_asset = store_binary_asset(
        category="document_original",
        filename="broken.pdf",
        content=b"%PDF-1.7 stale preview",
        document_id="d_preview_hwpx_parse_error",
        media_type="application/pdf",
    )

    DOCUMENTS["d_preview_hwpx_parse_error"] = {
        "documentId": "d_preview_hwpx_parse_error",
        "title": "broken",
        "sourceFilename": "broken.hwpx",
        "sourceFileType": "hwpx",
        "sourceFileId": source_asset["fileId"],
        "sourceFilePath": source_asset["storagePath"],
        "originalFilename": "broken.pdf",
        "originalFileId": pdf_asset["fileId"],
        "originalFilePath": pdf_asset["storagePath"],
        "originalStorageKind": "converted_pdf",
        "fileType": "pdf",
        "uploadedAt": "2026-03-25T00:00:00Z",
        "latestStatus": "COMPLETED",
        "jobId": "j_preview_hwpx_parse_error",
        "error": None,
    }

    response = client.get("/api/v1/documents/d_preview_hwpx_parse_error/original")
    assert response.status_code == 422
    assert response.json()["detail"]["error"]["code"] == "VALIDATION_ERROR"
    assert response.content != b"%PDF-1.7 stale preview"


def test_document_original_rejects_unsupported_binary(tmp_path: Path):
    client = make_client(tmp_path)

    input_path = tmp_path / "inputs" / "incoming" / "sample.bin"
    input_path.parent.mkdir(parents=True, exist_ok=True)
    input_path.write_bytes(b"binary")

    DOCUMENTS["d_preview_bin"] = {
        "documentId": "d_preview_bin",
        "title": "sample",
        "originalFilename": "sample.bin",
        "originalFilePath": str(input_path),
        "fileType": "bin",
        "uploadedAt": "2026-03-25T00:00:00Z",
        "latestStatus": "COMPLETED",
        "jobId": "j_preview_bin",
        "error": None,
    }

    response = client.get("/api/v1/documents/d_preview_bin/original")
    assert response.status_code == 422
    detail = response.json()["detail"]["error"]
    assert detail["code"] == "VALIDATION_ERROR"
    assert detail["details"]["previewKind"] == "unsupported"


def test_document_download_returns_404_when_original_file_is_missing(tmp_path: Path):
    client = make_client(tmp_path)

    missing_path = tmp_path / "inputs" / "missing.hwp"
    DOCUMENTS["d_missing_download"] = {
        "documentId": "d_missing_download",
        "title": "missing",
        "originalFilename": "missing.hwp",
        "originalFilePath": str(missing_path),
        "fileType": "hwp",
        "uploadedAt": "2026-03-25T00:00:00Z",
        "latestStatus": "FAILED",
        "jobId": "j_missing_download",
        "error": {"code": "CONVERSION_FAIL", "message": "boom"},
    }

    response = client.get("/api/v1/documents/d_missing_download/download")
    assert response.status_code == 404
    assert response.json()["detail"]["error"]["code"] == "FILE_MISSING"


def test_document_content_returns_full_original_text_for_csv(tmp_path: Path):
    client = make_client(tmp_path)

    input_path = tmp_path / "inputs" / "incoming" / "sample.csv"
    input_path.parent.mkdir(parents=True, exist_ok=True)
    original_text = "name,value\nalpha,1\nbeta,2\n"
    input_path.write_text(original_text, encoding="utf-8", newline="\n")

    DOCUMENTS["d_content_csv"] = {
        "documentId": "d_content_csv",
        "title": "sample",
        "originalFilename": "sample.csv",
        "originalFilePath": str(input_path),
        "fileType": "csv",
        "uploadedAt": "2026-03-25T00:00:00Z",
        "latestStatus": "QUEUED",
        "jobId": "j_content_csv",
        "error": None,
    }

    response = client.get("/api/v1/documents/d_content_csv/content")
    assert response.status_code == 200

    data = response.json()["data"]
    assert data["fileName"] == "sample.csv"
    assert data["content"] == original_text
    assert data["contentLength"] == len(original_text)
    assert data["extractionMethod"].startswith("text-read:")


def test_document_content_extracts_docx_text(tmp_path: Path):
    client = make_client(tmp_path)

    input_path = tmp_path / "inputs" / "incoming" / "sample.docx"
    input_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(input_path, "w") as archive:
        archive.writestr(
            "word/document.xml",
            (
                "<?xml version='1.0' encoding='UTF-8'?>"
                "<w:document xmlns:w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'>"
                "<w:body>"
                "<w:p><w:r><w:t>첫 번째 문단</w:t></w:r></w:p>"
                "<w:p><w:r><w:t>두 번째 문단</w:t></w:r></w:p>"
                "</w:body>"
                "</w:document>"
            ),
        )

    DOCUMENTS["d_content_docx"] = {
        "documentId": "d_content_docx",
        "title": "sample",
        "originalFilename": "sample.docx",
        "originalFilePath": str(input_path),
        "fileType": "docx",
        "uploadedAt": "2026-03-25T00:00:00Z",
        "latestStatus": "COMPLETED",
        "jobId": "j_content_docx",
        "error": None,
    }

    response = client.get("/api/v1/documents/d_content_docx/content")
    assert response.status_code == 200

    data = response.json()["data"]
    assert data["extractionMethod"] == "docx-ooxml"
    assert data["content"] == "첫 번째 문단\n\n두 번째 문단"


def test_document_content_returns_422_for_unsupported_binary_image(tmp_path: Path):
    client = make_client(tmp_path)

    input_path = tmp_path / "inputs" / "incoming" / "sample.png"
    input_path.parent.mkdir(parents=True, exist_ok=True)
    input_path.write_bytes(b"\x89PNG\r\n\x1a\n")

    DOCUMENTS["d_content_png"] = {
        "documentId": "d_content_png",
        "title": "sample",
        "originalFilename": "sample.png",
        "originalFilePath": str(input_path),
        "fileType": "png",
        "uploadedAt": "2026-03-25T00:00:00Z",
        "latestStatus": "QUEUED",
        "jobId": "j_content_png",
        "error": None,
    }

    response = client.get("/api/v1/documents/d_content_png/content")
    assert response.status_code == 422
    assert response.json()["detail"]["error"]["code"] == "VALIDATION_ERROR"

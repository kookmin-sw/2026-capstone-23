import importlib
import json
import os
import sys
import types
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("STATUS_CACHE_BACKEND", "none")

from infra.store import ADMIN_SETTINGS, DOCUMENTS, DOCUMENT_CACHE, JOBS
from storage.sqlite_files import clear_file_blobs


def make_client(tmp_path: Path) -> TestClient:
    def process_file(src: Path, language: str = "한국어", progress_callback=None):
        _ = language
        if progress_callback:
            progress_callback(None, 1, 3)
            progress_callback(None, 2, 3)
            progress_callback(None, 3, 3)
        out = tmp_path / "outputs" / f"{Path(src).stem}.txt"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(f"processed:{Path(src).name}", encoding="utf-8")
        out.with_suffix(".meta.json").write_text(
            json.dumps({"pageCount": 3}),
            encoding="utf-8",
        )
        return out

    deps_module = types.ModuleType("api.dependencies")
    deps_module.config = SimpleNamespace(
        input_root=tmp_path / "inputs",
        output_root=tmp_path / "outputs",
        tmp_root=tmp_path / "tmp",
        openai_model="gpt-5.2",
    )
    deps_module.pipeline = SimpleNamespace(
        update_vlm_model=lambda *_args, **_kwargs: None,
        process_file=process_file,
    )

    original_dependencies = sys.modules.get("api.dependencies")
    try:
        sys.modules["api.dependencies"] = deps_module
        import api.routers.parser as parser_module

        parser_module = importlib.reload(parser_module)
    finally:
        if original_dependencies is None:
            sys.modules.pop("api.dependencies", None)
        else:
            sys.modules["api.dependencies"] = original_dependencies

    app = FastAPI()
    app.include_router(parser_module.router, prefix="/api/v1")
    return TestClient(app)


def setup_function():
    ADMIN_SETTINGS.clear()
    DOCUMENTS.clear()
    DOCUMENT_CACHE.clear()
    JOBS.clear()
    clear_file_blobs()


def teardown_function():
    setup_function()


def test_parser_convert_keep_both_preserves_existing_file_and_reports_progress(tmp_path: Path):
    client = make_client(tmp_path)

    existing = tmp_path / "inputs" / "sample.pdf"
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_bytes(b"existing")

    convert_res = client.post(
        "/api/v1/parser/convert",
        data={
            "userId": "u-test",
            "modelId": "m1",
            "duplicatePolicy": "KEEP_BOTH",
            "parallelism": "1",
            "language": "한국어",
        },
        files=[("files", ("sample.pdf", b"%PDF-1.7\nnew-content", "application/pdf"))],
    )
    assert convert_res.status_code == 200

    item = convert_res.json()["data"]["items"][0]
    assert Path(item["originalFilePath"]).name == "sample_1.pdf"
    assert Path(item["txt"]["path"]).name == "sample_1.txt"
    assert existing.read_bytes() == b"existing"

    progress_res = client.get(f"/api/v1/parser/documents/{item['documentId']}/progress")
    assert progress_res.status_code == 200
    progress = progress_res.json()["data"]
    assert progress["status"] == "COMPLETED"
    assert progress["totalPages"] == 3
    assert progress["processedPages"] == 3
    assert progress["percent"] == 100


def test_parser_convert_publishes_websocket_progress_events(tmp_path: Path, monkeypatch):
    client = make_client(tmp_path)
    import api.routers.parser as parser_module

    events = []
    monkeypatch.setattr(parser_module, "publish_job_progress", events.append)

    convert_res = client.post(
        "/api/v1/parser/convert",
        data={
            "userId": "u-test",
            "modelId": "m1",
            "duplicatePolicy": "OVERWRITE",
            "parallelism": "1",
            "language": "한국어",
        },
        files=[("files", ("sample.pdf", b"%PDF-1.7\nnew-content", "application/pdf"))],
    )

    assert convert_res.status_code == 200
    result = convert_res.json()["data"]
    job_id = result["jobId"]
    document_id = result["items"][0]["documentId"]

    assert [event["eventType"] for event in events] == [
        "CREATED",
        "STARTED",
        "PROGRESS",
        "PROGRESS",
        "PROGRESS",
        "COMPLETED",
        "COMPLETED",
    ]
    assert events[0]["jobId"] == job_id
    assert events[0]["percent"] == 0
    assert events[1]["documentId"]
    assert events[2]["documentId"] == document_id
    assert events[2]["documentPercent"] == 33
    assert events[2]["jobPercent"] == 0
    assert events[4]["documentPercent"] == 100
    assert events[5]["documentId"] == document_id
    assert events[5]["status"] == "COMPLETED"
    assert events[5]["documentPercent"] == 100
    assert events[5]["jobPercent"] == 100
    assert events[-1]["documentId"] is None
    assert events[-1]["finishedDocuments"] == 1
    assert events[-1]["totalDocuments"] == 1


def test_parser_convert_persists_result_under_configured_storage_path(tmp_path: Path):
    client = make_client(tmp_path)
    configured_storage = tmp_path / "configured-storage"
    ADMIN_SETTINGS["storage"] = {"storagePath": str(configured_storage), "updatedAt": "2026-05-10T00:00:00Z"}

    convert_res = client.post(
        "/api/v1/parser/convert",
        data={
            "userId": "u-test",
            "modelId": "m1",
            "duplicatePolicy": "OVERWRITE",
            "parallelism": "1",
            "language": "한국어",
        },
        files=[("files", ("stored.pdf", b"%PDF-1.7\nnew-content", "application/pdf"))],
    )

    assert convert_res.status_code == 200
    item = convert_res.json()["data"]["items"][0]
    output_path = Path(item["txt"]["path"])

    assert output_path.is_relative_to(configured_storage)
    assert output_path.parts[-3] == "results"
    assert output_path.name == "stored.txt"
    assert output_path.exists()
    assert output_path.read_text(encoding="utf-8") == "processed:stored.pdf"
    assert output_path.with_suffix(".meta.json").exists()


def test_parser_document_progress_keeps_non_terminal_status_and_job_item_id(tmp_path: Path):
    client = make_client(tmp_path)

    DOCUMENTS["d_queued"] = {
        "documentId": "d_queued",
        "title": "sample",
        "originalFilename": "sample.pdf",
        "originalFilePath": str(tmp_path / "inputs" / "sample.pdf"),
        "fileType": "pdf",
        "uploadedAt": "2026-04-04T00:00:00Z",
        "updatedAt": "2026-04-04T00:01:00Z",
        "latestStatus": "QUEUED",
        "jobId": "j_queued",
        "jobItemId": "ji_queued",
        "processingTimeMs": None,
        "modelCode": "gpt-5.2",
        "outputPath": "",
        "error": None,
    }

    progress_res = client.get("/api/v1/parser/documents/d_queued/progress")
    assert progress_res.status_code == 200
    progress = progress_res.json()["data"]
    assert progress["jobItemId"] == "ji_queued"
    assert progress["status"] == "QUEUED"
    assert progress["percent"] == 0


def test_parser_convert_reuses_completed_result_within_cache_ttl(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DOCUMENT_CACHE_TTL_SECONDS", "3600")
    calls = {"count": 0}

    def process_file(src: Path, language: str = "한국어", progress_callback=None):
        _ = language
        if progress_callback:
            progress_callback(None, 1, 1)
        calls["count"] += 1
        out = tmp_path / "outputs" / f"{Path(src).stem}.txt"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(f"processed:{Path(src).name}:{calls['count']}", encoding="utf-8")
        return out

    deps_module = types.ModuleType("api.dependencies")
    deps_module.config = SimpleNamespace(
        input_root=tmp_path / "inputs",
        output_root=tmp_path / "outputs",
        tmp_root=tmp_path / "tmp",
        openai_model="gpt-5.2",
    )
    deps_module.pipeline = SimpleNamespace(
        update_vlm_model=lambda *_args, **_kwargs: None,
        process_file=process_file,
    )

    original_dependencies = sys.modules.get("api.dependencies")
    try:
        sys.modules["api.dependencies"] = deps_module
        import api.routers.parser as parser_module

        parser_module = importlib.reload(parser_module)
    finally:
        if original_dependencies is None:
            sys.modules.pop("api.dependencies", None)
        else:
            sys.modules["api.dependencies"] = original_dependencies

    app = FastAPI()
    app.include_router(parser_module.router, prefix="/api/v1")
    client = TestClient(app)

    first_res = client.post(
        "/api/v1/parser/convert",
        data={"modelId": "m1", "language": "한국어"},
        files=[("files", ("same.pdf", b"%PDF-1.7\nsame-content", "application/pdf"))],
    )
    assert first_res.status_code == 200
    first_item = first_res.json()["data"]["items"][0]
    assert first_item["cacheHit"] is False
    assert calls["count"] == 1

    second_res = client.post(
        "/api/v1/parser/convert",
        data={"modelId": "m1", "language": "한국어"},
        files=[("files", ("renamed.pdf", b"%PDF-1.7\nsame-content", "application/pdf"))],
    )
    assert second_res.status_code == 200
    second_item = second_res.json()["data"]["items"][0]
    assert second_item["cacheHit"] is True
    assert second_item["deduplicated"] is True
    assert second_item["documentId"] == first_item["documentId"]
    assert second_item["resultUrl"] == f"/api/v1/parser/documents/{first_item['documentId']}/result"
    assert calls["count"] == 1


def test_parser_retry_document_bypasses_cache_and_updates_existing_document(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DOCUMENT_CACHE_TTL_SECONDS", "3600")
    calls = {"count": 0}

    def process_file(src: Path, language: str = "한국어", progress_callback=None):
        _ = language
        if progress_callback:
            progress_callback(None, 1, 1)
        calls["count"] += 1
        out = tmp_path / "outputs" / f"{Path(src).stem}.txt"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(f"processed:{calls['count']}", encoding="utf-8")
        return out

    deps_module = types.ModuleType("api.dependencies")
    deps_module.config = SimpleNamespace(
        input_root=tmp_path / "inputs",
        output_root=tmp_path / "outputs",
        tmp_root=tmp_path / "tmp",
        openai_model="gpt-5.2",
    )
    deps_module.pipeline = SimpleNamespace(
        update_vlm_model=lambda *_args, **_kwargs: None,
        process_file=process_file,
    )

    original_dependencies = sys.modules.get("api.dependencies")
    try:
        sys.modules["api.dependencies"] = deps_module
        import api.routers.parser as parser_module

        parser_module = importlib.reload(parser_module)
    finally:
        if original_dependencies is None:
            sys.modules.pop("api.dependencies", None)
        else:
            sys.modules["api.dependencies"] = original_dependencies

    app = FastAPI()
    app.include_router(parser_module.router, prefix="/api/v1")
    client = TestClient(app)

    convert_res = client.post(
        "/api/v1/parser/convert",
        data={"modelId": "m1", "language": "한국어"},
        files=[("files", ("retry.pdf", b"%PDF-1.7\nretry-content", "application/pdf"))],
    )
    assert convert_res.status_code == 200
    document_id = convert_res.json()["data"]["items"][0]["documentId"]
    assert calls["count"] == 1

    retry_res = client.post(
        f"/api/v1/parser/documents/{document_id}/retry",
        json={"modelId": "m1", "language": "한국어"},
    )
    assert retry_res.status_code == 200
    retry = retry_res.json()["data"]
    assert retry["documentId"] == document_id
    assert retry["retried"] is True
    assert retry["ignoreCache"] is True
    assert retry["status"] == "COMPLETED"
    assert calls["count"] == 2

    result_res = client.get(f"/api/v1/parser/documents/{document_id}/result")
    assert result_res.status_code == 200
    assert result_res.json()["data"]["txt"]["preview"] == "processed:2"

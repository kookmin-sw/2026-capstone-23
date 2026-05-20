import importlib
import os
import sys
import types
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault("OPENAI_API_KEY", "test-key")

from infra.store import ADMIN_SETTINGS, DOCUMENTS, JOB_EVENTS, JOB_ITEMS, JOBS


def make_client(tmp_path: Path) -> TestClient:
    deps_module = types.ModuleType("api.dependencies")
    deps_module.config = SimpleNamespace(
        input_root=tmp_path / "inputs",
        output_root=tmp_path / "outputs",
        tmp_root=tmp_path / "tmp",
    )
    deps_module.get_config = lambda: deps_module.config
    deps_module.set_runtime_services = lambda **_: None

    original_dependencies = sys.modules.get("api.dependencies")
    try:
        sys.modules["api.dependencies"] = deps_module
        import api.routers.admin as admin_module
        import api.routers.monitoring as monitoring_module

        admin_module = importlib.reload(admin_module)
        monitoring_module = importlib.reload(monitoring_module)
    finally:
        if original_dependencies is None:
            sys.modules.pop("api.dependencies", None)
        else:
            sys.modules["api.dependencies"] = original_dependencies

    app = FastAPI()
    app.include_router(monitoring_module.router, prefix="/api/v1")
    app.include_router(admin_module.router, prefix="/api/v1")
    return TestClient(app)


def setup_function():
    ADMIN_SETTINGS.clear()
    DOCUMENTS.clear()
    JOBS.clear()
    JOB_ITEMS.clear()
    JOB_EVENTS.clear()


def teardown_function():
    setup_function()


def test_monitoring_error_list_and_detail(tmp_path: Path):
    client = make_client(tmp_path)

    JOBS["j_1"] = {"jobId": "j_1", "status": "FAILED", "modelId": "m1"}
    JOB_ITEMS["ji_1"] = {
        "jobItemId": "ji_1",
        "jobId": "j_1",
        "documentId": "d_1",
        "fileName": "diagram.webp",
        "sourcePath": "data/inputs/diagram.webp",
        "status": "FAILED",
        "retryCount": 1,
        "lastError": {"code": "CONVERSION_FAIL", "message": "unsupported image format"},
        "updatedAt": "2026-04-06T01:00:00Z",
    }
    JOB_EVENTS["e_1"] = {
        "eventId": "e_1",
        "jobId": "j_1",
        "jobItemId": "ji_1",
        "eventType": "FAILED",
        "status": "FAILED",
        "retryCount": 1,
        "error": {"code": "CONVERSION_FAIL", "message": "unsupported image format"},
        "createdAt": "2026-04-06T01:00:01Z",
    }

    list_res = client.get("/api/v1/monitoring/errors")
    assert list_res.status_code == 200
    data = list_res.json()["data"]
    assert data["total"] == 1
    assert data["items"][0]["type"] == "CONVERSION_FAIL"

    detail_res = client.get(f"/api/v1/monitoring/errors/{data['items'][0]['errorId']}")
    assert detail_res.status_code == 200
    detail = detail_res.json()["data"]
    assert detail["fileName"] == "diagram.webp"
    assert "recommendedActions" in detail


def test_monitoring_error_summary_tolerates_legacy_records_without_ids(tmp_path: Path):
    client = make_client(tmp_path)

    JOB_EVENTS["legacy_event"] = {
        "jobId": "j_legacy",
        "eventType": "FAILED",
        "status": "FAILED",
        "error": {"code": "LEGACY_EVENT_FAIL", "message": "legacy event failure"},
        "createdAt": "2026-04-06T01:00:01Z",
    }
    JOB_ITEMS["legacy_item"] = {
        "jobId": "j_legacy",
        "fileName": "legacy.pdf",
        "sourcePath": "data/inputs/legacy.pdf",
        "status": "FAILED",
        "lastError": {"code": "LEGACY_ITEM_FAIL", "message": "legacy item failure"},
        "updatedAt": "2026-04-06T01:00:00Z",
    }

    summary_res = client.get("/api/v1/monitoring/errors/summary")

    assert summary_res.status_code == 200
    payload = summary_res.json()
    assert payload["success"] is True
    assert payload["error"] is None
    data = payload["data"]
    assert data["totalErrors"] == 2
    assert data["byType"] == {
        "LEGACY_EVENT_FAIL": 1,
        "LEGACY_ITEM_FAIL": 1,
    }
    assert all(item["errorId"].startswith("err_") for item in data["recent"])


def test_admin_storage_get_and_update(tmp_path: Path):
    client = make_client(tmp_path)
    tmp_path.joinpath("outputs").mkdir()

    get_res = client.get("/api/v1/admin/storage")
    assert get_res.status_code == 200
    get_payload = get_res.json()
    assert get_payload["success"] is True
    assert get_payload["error"] is None
    assert get_payload["data"]["outputRoot"] == str(tmp_path / "outputs")
    assert get_payload["data"]["usage"]["usedBytes"] >= 0
    assert get_payload["data"]["usage"]["totalBytes"] > 0
    assert get_payload["data"]["usage"]["usagePercent"] >= 0

    storage_path = tmp_path / "archive"
    update_res = client.put("/api/v1/admin/storage", json={"storagePath": str(storage_path)})
    assert update_res.status_code == 200
    update_payload = update_res.json()
    assert update_payload["success"] is True
    assert update_payload["error"] is None
    updated = update_payload["data"]
    assert updated["storagePath"] == str(storage_path)
    assert updated["usage"]["path"] == str(storage_path)


def test_monitoring_storage_uses_configured_admin_path(tmp_path: Path):
    client = make_client(tmp_path)
    storage_path = tmp_path / "archive"

    update_res = client.put("/api/v1/admin/storage", json={"storagePath": str(storage_path)})
    assert update_res.status_code == 200

    monitoring_res = client.get("/api/v1/monitoring/system")
    assert monitoring_res.status_code == 200
    payload = monitoring_res.json()
    assert payload["success"] is True
    assert payload["error"] is None

    storage = payload["data"]["storage"]
    assert storage["output"]["path"] == str(storage_path)
    assert storage["output"]["exists"] is False
    assert storage["input"]["path"] == str(tmp_path / "inputs")
    assert storage["tmp"]["path"] == str(tmp_path / "tmp")

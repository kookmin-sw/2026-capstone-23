import os

from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault("OPENAI_API_KEY", "test-key")

from api.routers.dashboard import router as dashboard_router
from infra.store import DOCUMENTS


def make_client() -> TestClient:
    app = FastAPI()
    app.include_router(dashboard_router, prefix="/api/v1")
    return TestClient(app)


def setup_function():
    DOCUMENTS.clear()


def test_dashboard_summary_file_types_and_recent_items_are_draft_aligned():
    client = make_client()

    DOCUMENTS["d_old"] = {
        "documentId": "d_old",
        "originalFilename": "old.pdf",
        "fileType": "pdf",
        "uploadedAt": "2026-04-01T00:00:00Z",
        "updatedAt": "2026-04-01T00:00:00Z",
        "latestStatus": "QUEUED",
    }
    DOCUMENTS["d_done"] = {
        "documentId": "d_done",
        "originalFilename": "done.pdf",
        "fileType": "pdf",
        "uploadedAt": "2026-04-02T00:00:00Z",
        "updatedAt": "2026-04-02T00:00:00Z",
        "latestStatus": "COMPLETED",
    }
    DOCUMENTS["d_fail"] = {
        "documentId": "d_fail",
        "originalFilename": "fail.docx",
        "fileType": "docx",
        "uploadedAt": "2026-04-03T00:00:00Z",
        "updatedAt": "2026-04-03T00:00:00Z",
        "latestStatus": "FAILED",
    }
    DOCUMENTS["d_cancel"] = {
        "documentId": "d_cancel",
        "originalFilename": "cancel.pdf",
        "fileType": "pdf",
        "uploadedAt": "2026-04-04T00:00:00Z",
        "updatedAt": "2026-04-04T00:00:00Z",
        "latestStatus": "CANCELED",
    }

    summary_res = client.get("/api/v1/dashboard/summary")
    assert summary_res.status_code == 200
    summary = summary_res.json()["data"]
    assert summary == {
        "totalJobs": 4,
        "completedJobs": 1,
        "processingJobs": 1,
        "failedJobs": 1,
    }

    file_types_res = client.get("/api/v1/dashboard/file-types")
    assert file_types_res.status_code == 200
    file_types = file_types_res.json()["data"]
    assert file_types["types"] == ["docx", "pdf"]
    assert file_types["items"] == [
        {"type": "docx", "count": 1},
        {"type": "pdf", "count": 3},
    ]

    recent_res = client.get("/api/v1/dashboard/recent-items", params={"limit": 2})
    assert recent_res.status_code == 200
    recent_ids = [item["documentId"] for item in recent_res.json()["data"]["items"]]
    assert recent_ids == ["d_cancel", "d_fail"]

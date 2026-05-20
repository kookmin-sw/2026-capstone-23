from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routers import files as files_router_module
from infra.store import DOCUMENTS
from storage.sqlite_files import clear_file_blobs


def make_client() -> TestClient:
    app = FastAPI()
    app.include_router(files_router_module.router, prefix="/api/v1")
    return TestClient(app)


def setup_function():
    DOCUMENTS.clear()
    clear_file_blobs()


def test_files_upload_creates_document_records_with_frontend_payload():
    client = make_client()

    response = client.post(
        "/api/v1/files/upload",
        files=[("files", ("sample.pdf", b"%PDF-1.7", "application/pdf"))],
    )

    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["count"] == 1
    assert len(payload["items"]) == 1

    item = payload["items"][0]
    assert item["fileId"].startswith("d_")
    assert item["originalFilename"] == "sample.pdf"
    assert item["fileType"] == "pdf"
    assert item["sizeBytes"] == 8
    assert item["downloadUrl"] == f"/api/v1/files/download/{item['fileId']}"
    assert item["processable"] is True

    document = DOCUMENTS[item["fileId"]]
    assert document["latestStatus"] == "UPLOADED"
    assert document["sourceFilename"] == "sample.pdf"


def test_files_download_accepts_document_id():
    client = make_client()

    upload_response = client.post(
        "/api/v1/files/upload",
        files=[("files", ("sample.txt", b"hello", "text/plain"))],
    )
    document_id = upload_response.json()["data"]["items"][0]["fileId"]

    response = client.get(f"/api/v1/files/download/{document_id}")

    assert response.status_code == 200
    assert response.content == b"hello"
    assert response.headers["content-type"].startswith("text/plain")
    assert "sample.txt" in response.headers["content-disposition"]


def test_files_download_accepts_stored_asset_id():
    client = make_client()

    upload_response = client.post(
        "/api/v1/files/upload",
        files=[("files", ("sample.txt", b"hello", "text/plain"))],
    )
    document_id = upload_response.json()["data"]["items"][0]["fileId"]
    asset_id = DOCUMENTS[document_id]["sourceFileId"]

    response = client.get(f"/api/v1/files/download/{asset_id}")

    assert response.status_code == 200
    assert response.content == b"hello"

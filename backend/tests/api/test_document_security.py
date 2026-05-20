from __future__ import annotations

import json

import pytest
from defusedxml.common import DefusedXmlException
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.services.original_content import _extract_paragraphs_from_xml_bytes
from api.routers.admin import router as admin_router
from api.routers.documents import router as documents_router
from api.routers.files import router as files_router
from db.models.store import StoredFile
from db.session import SessionLocal
from infra.storage.file_assets import read_binary_asset, store_binary_asset
from infra.store import AUDIT_LOGS, DOCUMENTS
from storage.sqlite_files import clear_file_blobs


def make_client() -> TestClient:
    app = FastAPI()
    app.include_router(files_router, prefix="/api/v1")
    app.include_router(documents_router, prefix="/api/v1")
    app.include_router(admin_router, prefix="/api/v1")
    return TestClient(app)


def setup_function():
    DOCUMENTS.clear()
    AUDIT_LOGS.clear()
    clear_file_blobs()


def test_stored_file_payload_is_encrypted_at_rest():
    asset = store_binary_asset(
        category="document_source",
        filename="secret.txt",
        content=b"confidential text",
        document_id="d_secret",
    )

    with SessionLocal() as session:
        row = session.get(StoredFile, asset["fileId"])
        assert row is not None
        assert bytes(row.payload) != b"confidential text"
        metadata = json.loads(row.metadata_json)
        assert metadata["_encryption"]["algorithm"] == "AES-256-GCM"
        assert row.size_bytes == len(b"confidential text")

    loaded = read_binary_asset(asset["fileId"])
    assert loaded["content"] == b"confidential text"
    assert loaded["sizeBytes"] == len(b"confidential text")


def test_upload_rejects_extension_signature_mismatch_and_audits_denial():
    client = make_client()

    response = client.post(
        "/api/v1/files/upload",
        files=[("files", ("fake.pdf", b"not a pdf", "application/pdf"))],
    )

    assert response.status_code == 415
    assert response.json()["detail"]["error"]["code"] == "INVALID_FILE_SIGNATURE"
    events = list(AUDIT_LOGS.values())
    assert len(events) == 1
    assert events[0]["action"] == "DOCUMENT_UPLOAD_REJECTED"
    assert events[0]["outcome"] == "DENIED"


def test_ooxml_text_extraction_rejects_xml_entities():
    malicious_xml = b"""<?xml version="1.0"?>
<!DOCTYPE document [
  <!ENTITY local SYSTEM "file:///etc/passwd">
]>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body><w:p><w:r><w:t>&local;</w:t></w:r></w:p></w:body>
</w:document>
"""

    with pytest.raises(DefusedXmlException):
        _extract_paragraphs_from_xml_bytes(
            malicious_xml,
            paragraph_tags={"p"},
            text_tags={"t"},
        )


def test_document_disposal_deletes_assets_cache_and_records_audit_log():
    client = make_client()

    upload_response = client.post(
        "/api/v1/files/upload",
        files=[("files", ("sample.pdf", b"%PDF-1.7\nbody", "application/pdf"))],
    )
    assert upload_response.status_code == 200
    document_id = upload_response.json()["data"]["items"][0]["fileId"]
    source_file_id = DOCUMENTS[document_id]["sourceFileId"]

    assert read_binary_asset(source_file_id)["content"] == b"%PDF-1.7\nbody"

    delete_response = client.delete(f"/api/v1/documents/{document_id}")
    assert delete_response.status_code == 200
    assert delete_response.json()["data"]["fileExisted"] is True
    assert document_id not in DOCUMENTS

    try:
        read_binary_asset(source_file_id)
        raise AssertionError("disposed file should be deleted")
    except FileNotFoundError:
        pass

    audit_response = client.get("/api/v1/admin/audit-logs", params={"resourceId": document_id})
    assert audit_response.status_code == 200
    actions = {event["action"] for event in audit_response.json()["data"]["items"]}
    assert {"DOCUMENT_UPLOADED", "DOCUMENT_DISPOSED"}.issubset(actions)

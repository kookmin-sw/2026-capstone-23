from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import quote

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import Response

from api.common import fail, ok
from api.security import current_user_id, require_record_access
from api.services.audit_log import record_audit_event
from api.services.document_files import create_uploaded_document
from api.services.upload_security import UploadSecurityError, read_upload_file_secure
from core.documents.records import normalize_filename
from infra.storage.file_assets import read_binary_asset
from infra.store import DOCUMENTS, SUPPORTED_TYPES


router = APIRouter(prefix="/files", tags=["files"])


def _content_disposition_header(filename: str, disposition_type: str = "attachment") -> str:
    normalized = normalize_filename(filename)
    ascii_name = normalized.encode("ascii", "ignore").decode("ascii") or "download"
    return f"{disposition_type}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(normalized)}"


def _uploaded_file_payload(record: Dict[str, Any], *, size_bytes: int) -> Dict[str, Any]:
    document_id = str(record["documentId"])
    file_type = str(record.get("sourceFileType") or record.get("fileType") or "unknown")
    return {
        "fileId": document_id,
        "originalFilename": record.get("sourceFilename") or record.get("originalFilename"),
        "fileType": file_type,
        "sizeBytes": size_bytes,
        "uploadedAt": record.get("uploadedAt"),
        "downloadUrl": f"/api/v1/files/download/{document_id}",
        "processable": f".{file_type.lower().lstrip('.')}" in SUPPORTED_TYPES,
    }


@router.post(
    "/upload",
    summary="Upload files",
    description=(
        "Compatibility endpoint for the frontend file upload API. Uploaded files "
        "are stored as document records so the existing document list/detail APIs "
        "can read them without frontend changes."
    ),
)
async def upload_files(request: Request, files: List[UploadFile] = File(...)):
    uploaded: List[Dict[str, Any]] = []
    owner_user_id = current_user_id(request)

    for upload in files:
        try:
            filename, content, security = await read_upload_file_secure(upload)
        except UploadSecurityError as exc:
            record_audit_event(
                action="DOCUMENT_UPLOAD_REJECTED",
                resource_type="upload",
                resource_id=normalize_filename(upload.filename or "unnamed"),
                outcome="DENIED",
                request=request,
                details={"code": exc.code, **exc.details},
            )
            fail(exc.code, exc.message, status=exc.status, details=exc.details)

        document_id = f"d_{uuid.uuid4().hex[:10]}"
        record, _assets = create_uploaded_document(
            document_id=document_id,
            filename=filename,
            content=content,
            status="UPLOADED",
            convert_preview=False,
            owner_user_id=owner_user_id,
        )
        DOCUMENTS[document_id] = record
        record_audit_event(
            action="DOCUMENT_UPLOADED",
            resource_type="document",
            resource_id=document_id,
            request=request,
            details={"filename": filename, "sizeBytes": security["sizeBytes"], "endpoint": "files.upload"},
        )
        uploaded.append(_uploaded_file_payload(record, size_bytes=len(content)))

    return ok({"count": len(uploaded), "items": uploaded})


@router.get(
    "/download/{fileId}",
    summary="Download file",
    description=(
        "Downloads a file by documentId for frontend compatibility. Stored asset "
        "ids are also accepted for internal tooling."
    ),
)
def download_file(request: Request, fileId: str):
    record = DOCUMENTS.get(fileId)
    if record is not None:
        require_record_access(request, record, resource="document", resource_id=fileId)
        asset_id = str(record.get("sourceFileId") or record.get("originalFileId") or "")
        filename = str(record.get("sourceFilename") or record.get("originalFilename") or fileId)
        if not asset_id:
            fail("FILE_MISSING", f"stored file is missing: {fileId}", status=404)
        try:
            asset = read_binary_asset(asset_id)
        except FileNotFoundError:
            fail("FILE_MISSING", f"stored file is missing: {fileId}", status=404)
        record_audit_event(
            action="DOCUMENT_DOWNLOADED",
            resource_type="document",
            resource_id=fileId,
            request=request,
            details={"filename": filename, "assetId": asset_id, "endpoint": "files.download"},
        )
        return Response(
            content=bytes(asset["content"]),
            media_type=str(asset.get("mediaType") or "application/octet-stream"),
            headers={
                "Content-Disposition": _content_disposition_header(filename),
                "X-Content-Type-Options": "nosniff",
            },
        )

    try:
        asset = read_binary_asset(fileId)
    except FileNotFoundError:
        fail("NOT_FOUND", f"file not found: {fileId}", status=404)

    document_id = str(asset.get("documentId") or "")
    if document_id:
        linked_record = DOCUMENTS.get(document_id)
        if linked_record is not None:
            require_record_access(request, linked_record, resource="document", resource_id=document_id)

    filename = str(asset.get("filename") or Path(fileId).name or "download")
    record_audit_event(
        action="FILE_DOWNLOADED",
        resource_type="stored_file",
        resource_id=fileId,
        request=request,
        details={"filename": filename, "documentId": document_id or None, "endpoint": "files.download"},
    )
    return Response(
        content=bytes(asset["content"]),
        media_type=str(asset.get("mediaType") or "application/octet-stream"),
        headers={
            "Content-Disposition": _content_disposition_header(filename),
            "X-Content-Type-Options": "nosniff",
        },
    )

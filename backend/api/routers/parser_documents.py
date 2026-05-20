from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Request

from api.common import fail, fail_not_found, now_iso, ok
from api.dependencies import config, pipeline
from api.schemas.parser import DocumentRetryRequest
from api.security import current_user_id, require_record_access
from api.services.audit_log import record_audit_event
from api.services.parser_helpers import (
    PROGRESS_PERCENT_BY_STATUS as _PROGRESS_PERCENT_BY_STATUS,
    cache_expiry as _cache_expiry,
    cache_key as _cache_key,
    cache_ttl_seconds as _cache_ttl_seconds,
    coerce_int as _coerce_int,
    load_output_meta_with_doc as _load_output_meta_with_doc,
    result_url as _result_url,
)
from core.documents.records import build_document_record
from core.documents.storage import (
    cleanup_output_artifacts,
    persist_result_artifact,
    result_filename_for,
)
from core.model_catalog import find_model_code
from infra.storage.file_assets import (
    delete_binary_asset,
    load_output_text,
    materialize_record_asset,
)
from infra.storage.settings import get_configured_storage_path
from infra.store import DOCUMENTS, DOCUMENT_CACHE, JOBS


router = APIRouter(tags=["parser"])


def _require_document_access(
    request: Request | None, document_id: str
) -> Dict[str, Any]:
    doc = DOCUMENTS.get(document_id)
    if not doc:
        fail_not_found("document", document_id)
    require_record_access(request, doc, resource="document", resource_id=document_id)
    return doc


@router.get(
    "/documents/{documentId}/progress",
    summary="Document progress",
    description="Returns the current processing progress for one document.",
)
def get_document_progress(request: Request, documentId: str):
    doc = _require_document_access(request, documentId)

    status = str(doc.get("latestStatus") or "UPLOADED")
    meta = _load_output_meta_with_doc(doc)
    total_pages = _coerce_int(meta.get("totalPages") or meta.get("pageCount"))
    processed_pages = _coerce_int(meta.get("processedPages"))

    if status == "COMPLETED" and total_pages is not None and processed_pages is None:
        processed_pages = total_pages

    progress_percent = _coerce_int(meta.get("progressPercent"))
    if total_pages and processed_pages is not None and total_pages > 0:
        percent = min(100, max(0, int((processed_pages / total_pages) * 100)))
    elif progress_percent is not None:
        percent = min(100, max(0, progress_percent))
    else:
        percent = _PROGRESS_PERCENT_BY_STATUS.get(status, 0)

    return ok(
        {
            "documentId": documentId,
            "jobId": doc.get("jobId"),
            "jobItemId": doc.get("jobItemId") or f"ji_{documentId}",
            "status": status,
            "totalPages": total_pages or 0,
            "processedPages": processed_pages or 0,
            "percent": percent,
            "progressPercent": percent,
            "updatedAt": doc.get("updatedAt") or now_iso(),
        }
    )


@router.post(
    "/documents/{documentId}/retry",
    summary="Retry document",
    description="Reprocesses one document from its stored source asset and updates its latest result.",
)
def retry_document(request: Request, documentId: str, req: DocumentRetryRequest):
    doc = _require_document_access(request, documentId)
    owner_user_id = str(doc.get("ownerUserId") or current_user_id(request) or "")

    model_code = find_model_code(req.modelId, config.openai_model)
    pipeline.update_vlm_model(model_code)
    ttl_seconds = _cache_ttl_seconds()
    job_id = f"j_{uuid.uuid4().hex[:10]}"
    started_at = datetime.now(timezone.utc)

    JOBS[job_id] = {
        "jobId": job_id,
        "ownerUserId": owner_user_id or None,
        "status": "PROCESSING",
        "cancelRequested": False,
        "canceledAt": None,
        "totalDocuments": 1,
        "completedDocuments": 0,
        "failedDocuments": 0,
        "processingDocuments": 1,
        "pendingDocuments": 0,
        "completedDocumentIds": [],
        "updatedAt": now_iso(),
    }

    status = "COMPLETED"
    output_txt_path = ""
    output_file_id: Optional[str] = None
    output_meta: Dict[str, Any] = {}
    error_info = None
    file_sha256 = ""
    cache_key = ""
    try:
        with materialize_record_asset(
            doc,
            file_id_key="sourceFileId",
            filename_key="sourceFilename",
            fallback_path_key="sourceFilePath",
            tmp_root=config.tmp_root,
            purpose="retry",
            owner_id=documentId,
        ) as source_path:
            file_bytes = source_path.read_bytes()
            file_sha256 = hashlib.sha256(file_bytes).hexdigest()
            cache_key = _cache_key(
                file_sha256,
                model_code,
                req.language,
                owner_user_id=owner_user_id or None,
            )
            out = pipeline.process_file(source_path, language=req.language)
            stored_output = persist_result_artifact(
                document_id=documentId,
                output_path=out,
                output_filename=result_filename_for(
                    str(
                        doc.get("sourceFilename")
                        or doc.get("originalFilename")
                        or source_path.name
                    )
                ),
                display_source=str(
                    doc.get("sourceFilename")
                    or doc.get("originalFilename")
                    or doc.get("sourceFilePath")
                    or doc.get("originalFilePath")
                    or ""
                ),
                display_pdf_path=(
                    str(doc.get("originalFilePath") or "")
                    if str(doc.get("fileType") or "").lower() == "pdf"
                    else None
                ),
                storage_root=get_configured_storage_path(config.output_root),
            )
            output_txt_path = stored_output["storagePath"]
            output_file_id = stored_output["fileId"]
            output_meta = dict(stored_output["meta"])
            cleanup_output_artifacts(out)
    except FileNotFoundError as exc:
        fail(
            "SOURCE_FILE_MISSING",
            f"original file is missing: {documentId}",
            status=404,
            details={
                "documentId": documentId,
                "originalFilePath": str(
                    doc.get("sourceFilePath") or doc.get("originalFilePath") or ""
                ),
                "reason": str(exc),
            },
        )
    except Exception as exc:  # noqa: BLE001
        status = "FAILED"
        error_info = {"code": "CONVERSION_FAIL", "message": str(exc)}

    elapsed_ms = int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000)
    previous_output_file_id = str(doc.get("outputFileId") or "")
    if previous_output_file_id and previous_output_file_id != output_file_id:
        delete_binary_asset(previous_output_file_id)
    updated_doc = build_document_record(
        documentId,
        str(doc.get("originalFilename") or doc.get("sourceFilename") or "document"),
        status=status,
        original_file_path=str(doc.get("originalFilePath") or ""),
        original_file_id=str(doc.get("originalFileId") or "") or None,
        original_storage_kind=str(doc.get("originalStorageKind") or "") or None,
        source_filename=str(doc.get("sourceFilename") or ""),
        source_file_path=str(doc.get("sourceFilePath") or ""),
        source_file_id=str(doc.get("sourceFileId") or "") or None,
        uploaded_at=str(doc.get("uploadedAt") or now_iso()),
        updated_at=now_iso(),
        job_id=job_id,
        job_item_id=str(doc.get("jobItemId") or ""),
        processing_time_ms=elapsed_ms,
        model_code=model_code,
        file_sha256=file_sha256,
        cache_key=cache_key,
        cache_expires_at=_cache_expiry(ttl_seconds),
        meta=output_meta,
        output_path=output_txt_path,
        output_file_id=output_file_id,
        output_filename=result_filename_for(
            str(doc.get("sourceFilename") or doc.get("originalFilename") or "document")
        ),
        owner_user_id=owner_user_id or None,
        error=error_info,
    )
    DOCUMENTS[documentId] = updated_doc

    if status == "COMPLETED":
        job_status = "COMPLETED"
        JOBS[job_id]["completedDocuments"] = 1
        JOBS[job_id]["completedDocumentIds"] = [documentId]
        if ttl_seconds > 0 and output_txt_path:
            DOCUMENT_CACHE[cache_key] = {
                "cacheKey": cache_key,
                "fileSha256": file_sha256,
                "documentId": documentId,
                "modelCode": model_code,
                "language": req.language,
                "ownerUserId": owner_user_id or None,
                "outputFileId": output_file_id,
                "outputPath": output_txt_path,
                "resultUrl": _result_url(documentId),
                "createdAt": now_iso(),
                "expiresAt": _cache_expiry(ttl_seconds),
            }
    else:
        job_status = "FAILED"
        JOBS[job_id]["failedDocuments"] = 1

    job = JOBS[job_id]
    job["status"] = job_status
    job["processingDocuments"] = 0
    job["pendingDocuments"] = 0
    job["updatedAt"] = now_iso()
    JOBS[job_id] = job
    record_audit_event(
        action="DOCUMENT_RETRIED",
        resource_type="document",
        resource_id=documentId,
        request=request,
        details={"jobId": job_id, "status": status, "modelCode": model_code},
    )

    return ok(
        {
            "documentId": documentId,
            "jobId": job_id,
            "status": status,
            "retried": True,
            "ignoreCache": True,
            "resultUrl": _result_url(documentId),
            "txt": {"path": output_txt_path},
            "processingTimeMs": elapsed_ms,
            "error": error_info,
        }
    )


@router.get(
    "/documents/{documentId}/result",
    summary="Document result",
    description="Returns full text, metadata, cache, and error details for one processed document.",
)
def get_document_result(request: Request, documentId: str):
    doc = _require_document_access(request, documentId)

    status = doc.get("latestStatus")
    output_path_value = str(doc.get("outputPath") or "")
    preview = load_output_text(doc)
    meta = _load_output_meta_with_doc(doc)
    record_audit_event(
        action="DOCUMENT_RESULT_READ",
        resource_type="document",
        resource_id=documentId,
        request=request,
        details={"status": status, "endpoint": "parser.document.result"},
    )

    return ok(
        {
            "documentId": documentId,
            "status": status,
            "fileName": doc.get("sourceFilename") or doc.get("originalFilename"),
            "modelCode": doc.get("modelCode"),
            "txt": {"path": output_path_value, "preview": preview},
            "resultUrl": _result_url(documentId),
            "htmlPreview": None,
            "markdown": None,
            "imageDescriptions": [],
            "meta": meta,
            "cache": {
                "fileSha256": doc.get("fileSha256"),
                "cacheKey": doc.get("cacheKey"),
                "cacheExpiresAt": doc.get("cacheExpiresAt"),
            },
            "error": doc.get("error"),
        }
    )

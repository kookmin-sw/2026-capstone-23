from __future__ import annotations

import mimetypes
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, Response

from api.common import ErrorCode, fail, now_iso, ok
from api.dependencies import config
from api.routers.parser import get_document_result
from api.security import current_user_id, filter_records_for_user, require_record_access
from api.services.audit_log import record_audit_event
from api.services.docx_preview import render_docx_preview_html
from api.services.document_disposal import dispose_document_record
from api.services.document_files import create_uploaded_document
from api.services.document_preview import (
    detect_document_preview_kind,
    detect_preview_kind,
    resolve_preview_media_type,
    supports_generated_html_preview,
)
from core.documents.hwpx_preview import render_hwpx_preview_html
from core.documents.records import (
    document_record_for_display,
    normalize_filename,
)
from core.documents.storage import (
    can_generate_pdf_preview_from_source,
    generate_pdf_preview_asset_for_record,
)
from api.services.original_content import extract_original_document_content
from api.services.upload_security import UploadSecurityError, read_upload_file_secure
from core.vlm_client import VLMClient
from infra.storage.file_assets import (
    delete_binary_asset,
    load_record_asset,
    materialize_record_asset,
)
from infra.store import DOCUMENTS, SUPPORTED_TYPES


router = APIRouter(prefix="/documents", tags=["documents"])
_hwpx_preview_vlm_client: VLMClient | None = None


def _get_hwpx_preview_vlm_client() -> VLMClient:
    global _hwpx_preview_vlm_client
    if _hwpx_preview_vlm_client is None:
        _hwpx_preview_vlm_client = VLMClient(
            openai_model=config.openai_model,
            device=config.vlm_device,
            max_concurrent_api=config.vlm_max_concurrent,
            gpu_max_concurrent=config.gpu_max_concurrent,
        )
    return _hwpx_preview_vlm_client


def _describe_hwpx_preview_image(
    image_bytes: bytes, _mime_type: str, _image_ref: str
) -> str:
    result = _get_hwpx_preview_vlm_client().describe_image(
        image_bytes,
        language="한국어",
        is_table=None,
        is_flowchart=None,
        is_math=None,
    )
    return str(result.get("text") or "").strip()


def _get_document_item(
    document_id: str, request: Request | None = None
) -> Dict[str, Any]:
    item = DOCUMENTS.get(document_id)
    if not item:
        fail("NOT_FOUND", f"document not found: {document_id}", status=404)
    require_record_access(request, item, resource="document", resource_id=document_id)
    return item


def _resolve_legacy_file_path(
    document_id: str, item: Dict[str, Any], *, field_name: str, filename_field: str
) -> Path:
    stored_path = item.get(field_name)
    file_path = (
        Path(str(stored_path))
        if stored_path
        else config.input_root / (item.get(filename_field) or document_id)
    )
    if not file_path.exists() or not file_path.is_file():
        fail(
            "FILE_MISSING",
            f"stored file is missing: {document_id}",
            status=404,
            details={"documentId": document_id, "storedPath": str(file_path)},
        )
    return file_path


def _content_disposition_header(filename: str, disposition_type: str) -> str:
    normalized = normalize_filename(filename)
    ascii_name = normalized.encode("ascii", "ignore").decode("ascii") or "download"
    return f"{disposition_type}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(normalized)}"


def _build_document_bytes_response(
    *,
    content: bytes,
    filename: str,
    media_type: str,
    content_disposition_type: str,
) -> Response:
    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Content-Disposition": _content_disposition_header(
                filename, content_disposition_type
            ),
            "X-Content-Type-Options": "nosniff",
        },
    )


def _build_document_file_response(
    file_path: Path,
    item: Dict[str, Any],
    *,
    media_type: str,
    content_disposition_type: str,
    filename_field: str = "originalFilename",
) -> FileResponse:
    return FileResponse(
        path=file_path,
        media_type=media_type,
        filename=str(
            item.get(filename_field) or item.get("originalFilename") or file_path.name
        ),
        content_disposition_type=content_disposition_type,
        headers={"X-Content-Type-Options": "nosniff"},
    )


def _load_original_asset(item: Dict[str, Any]) -> Dict[str, Any] | None:
    return load_record_asset(
        item, file_id_key="originalFileId", filename_key="originalFilename"
    )


def _load_source_asset(item: Dict[str, Any]) -> Dict[str, Any] | None:
    return load_record_asset(
        item, file_id_key="sourceFileId", filename_key="sourceFilename"
    )


def _build_generated_html_preview_response(
    document_id: str, item: Dict[str, Any]
) -> Response:
    source_name = str(
        item.get("sourceFilename")
        or item.get("originalFilename")
        or f"{document_id}.html"
    )
    preview_filename = f"{Path(normalize_filename(source_name)).stem}.html"
    source_suffix = Path(source_name).suffix.lower()

    try:
        with materialize_record_asset(
            item,
            file_id_key="sourceFileId",
            filename_key="sourceFilename",
            fallback_path_key="sourceFilePath",
            tmp_root=config.tmp_root,
            purpose="preview_html",
            owner_id=document_id,
        ) as source_path:
            source_bytes = source_path.read_bytes()
            if source_suffix == ".docx":
                html_content = render_docx_preview_html(
                    source_bytes, filename=source_name
                )
            elif source_suffix == ".hwpx":
                html_content = render_hwpx_preview_html(
                    source_bytes,
                    filename=source_name,
                    describe_image=_describe_hwpx_preview_image,
                )
            else:
                raise ValueError(
                    f"html preview is not supported for this file type: {source_suffix}"
                )
    except FileNotFoundError as exc:
        fail(
            "FILE_MISSING",
            f"stored file is missing: {document_id}",
            status=404,
            details={
                "documentId": document_id,
                "storedPath": str(
                    item.get("sourceFilePath") or item.get("originalFilePath") or ""
                ),
                "reason": str(exc),
            },
        )
    except Exception as exc:  # noqa: BLE001
        fail(
            ErrorCode.VALIDATION_ERROR,
            str(exc),
            status=422,
            details={
                "documentId": document_id,
                "fileType": item.get("fileType"),
                "previewKind": "html",
            },
        )

    return _build_document_bytes_response(
        content=html_content.encode("utf-8"),
        filename=preview_filename,
        media_type="text/html; charset=utf-8",
        content_disposition_type="inline",
    )


def _ensure_previewable_original_item(
    document_id: str, item: Dict[str, Any]
) -> Dict[str, Any]:
    source_name = str(
        item.get("sourceFilename") or item.get("originalFilename") or document_id
    )
    if Path(normalize_filename(source_name)).suffix.lower() == ".hwpx":
        return item

    current_name = str(
        item.get("originalFilename") or item.get("sourceFilename") or document_id
    )
    if resolve_preview_media_type(Path(current_name)) is not None:
        return item

    if not can_generate_pdf_preview_from_source(source_name):
        return item

    try:
        generated_asset = generate_pdf_preview_asset_for_record(
            record=item,
            document_id=document_id,
            tmp_root=config.tmp_root,
        )
    except Exception as exc:  # noqa: BLE001
        if supports_generated_html_preview(source_name):
            return item
        fail(
            ErrorCode.VALIDATION_ERROR,
            str(exc),
            status=422,
            details={
                "documentId": document_id,
                "fileType": item.get("fileType"),
                "previewKind": "unsupported",
            },
        )

    previous_original_file_id = str(item.get("originalFileId") or "")
    source_file_id = str(item.get("sourceFileId") or "")

    updated_item = dict(item)
    updated_item["originalFileId"] = str(generated_asset["fileId"])
    updated_item["originalFilename"] = str(generated_asset["filename"])
    updated_item["originalFilePath"] = str(generated_asset["storagePath"])
    updated_item["originalStorageKind"] = "converted_pdf"
    updated_item["fileType"] = "pdf"
    updated_item["updatedAt"] = now_iso()
    DOCUMENTS[document_id] = updated_item

    if previous_original_file_id and previous_original_file_id not in {
        source_file_id,
        str(generated_asset["fileId"]),
    }:
        delete_binary_asset(previous_original_file_id)

    return updated_item


@router.get(
    "",
    summary="문서 목록 조회",
    description="저장된 문서 메타데이터 목록을 페이지 단위로 반환합니다. 최근 업로드 목록이나 문서 관리 화면에서 사용합니다.",
)
def list_documents(
    request: Request,
    limit: Optional[int] = Query(default=None, ge=1, le=10000),
    cursor: Optional[str] = None,
):
    _ = cursor
    visible_documents = filter_records_for_user(request, DOCUMENTS.values())
    items = sorted(
        visible_documents,
        key=lambda item: (
            str(item.get("updatedAt") or item.get("uploadedAt") or ""),
            str(item.get("documentId") or ""),
        ),
        reverse=True,
    )
    if limit is not None:
        items = items[:limit]
    return ok(
        {
            "items": [document_record_for_display(item) for item in items],
            "nextCursor": None,
        }
    )


@router.post(
    "/upload",
    summary="문서 업로드",
    description="문서를 서버 입력 경로에 저장하고 문서 메타데이터를 등록합니다. 변환 Job 생성 전 사전 업로드 흐름이 필요할 때 사용합니다.",
)
async def upload_documents(request: Request, files: List[UploadFile] = File(...)):
    uploaded: List[Dict[str, Any]] = []
    owner_user_id = current_user_id(request)
    for upload in files:
        try:
            source_filename, file_bytes, security = await read_upload_file_secure(
                upload
            )
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

        doc_id = f"d_{uuid.uuid4().hex[:10]}"
        row, _assets = create_uploaded_document(
            document_id=doc_id,
            filename=source_filename,
            content=file_bytes,
            tmp_root=config.tmp_root,
            status="UPLOADED",
            owner_user_id=owner_user_id,
        )
        DOCUMENTS[doc_id] = row
        record_audit_event(
            action="DOCUMENT_UPLOADED",
            resource_type="document",
            resource_id=doc_id,
            request=request,
            details={
                "filename": source_filename,
                "sizeBytes": security["sizeBytes"],
                "endpoint": "documents.upload",
            },
        )
        uploaded.append(document_record_for_display(row))

    return ok({"items": uploaded})


@router.get(
    "/{documentId}/download",
    summary="문서 원본 다운로드",
    description="저장된 문서의 원본 파일을 documentId 기준으로 다시 내려줍니다. 업로드 문서 확인이나 원본 다운로드 버튼에 사용합니다.",
)
def download_document_original(request: Request, documentId: str):
    item = _get_document_item(documentId, request)
    asset = _load_source_asset(item)
    if asset is not None:
        filename = str(item.get("sourceFilename") or asset["filename"])
        record_audit_event(
            action="DOCUMENT_DOWNLOADED",
            resource_type="document",
            resource_id=documentId,
            request=request,
            details={"filename": filename, "assetId": asset.get("fileId")},
        )
        return _build_document_bytes_response(
            content=bytes(asset["content"]),
            filename=filename,
            media_type=str(asset.get("mediaType") or "application/octet-stream"),
            content_disposition_type="attachment",
        )

    source_path_value = item.get("sourceFilePath")
    if source_path_value:
        source_path = Path(str(source_path_value))
        if source_path.exists() and source_path.is_file():
            filename = str(item.get("sourceFilename") or source_path.name)
            media_type = (
                mimetypes.guess_type(source_path.name)[0] or "application/octet-stream"
            )
            record_audit_event(
                action="DOCUMENT_DOWNLOADED",
                resource_type="document",
                resource_id=documentId,
                request=request,
                details={"filename": filename, "legacyPath": str(source_path)},
            )
            return _build_document_file_response(
                source_path,
                item,
                media_type=media_type,
                content_disposition_type="attachment",
                filename_field="sourceFilename",
            )

    asset = _load_original_asset(item)
    if asset is not None:
        filename = str(item.get("originalFilename") or asset["filename"])
        record_audit_event(
            action="DOCUMENT_DOWNLOADED",
            resource_type="document",
            resource_id=documentId,
            request=request,
            details={"filename": filename, "assetId": asset.get("fileId")},
        )
        return _build_document_bytes_response(
            content=bytes(asset["content"]),
            filename=filename,
            media_type=str(asset.get("mediaType") or "application/octet-stream"),
            content_disposition_type="attachment",
        )

    file_path = _resolve_legacy_file_path(
        documentId,
        item,
        field_name="originalFilePath",
        filename_field="originalFilename",
    )
    media_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    record_audit_event(
        action="DOCUMENT_DOWNLOADED",
        resource_type="document",
        resource_id=documentId,
        request=request,
        details={
            "filename": str(item.get("originalFilename") or file_path.name),
            "legacyPath": str(file_path),
        },
    )
    return _build_document_file_response(
        file_path,
        item,
        media_type=media_type,
        content_disposition_type="attachment",
    )


def _preview_document_original_file(documentId: str, request: Request | None = None):
    item = _get_document_item(documentId, request)
    source_name = str(
        item.get("sourceFilename") or item.get("originalFilename") or documentId
    )
    is_hwpx_source = Path(normalize_filename(source_name)).suffix.lower() == ".hwpx"

    item = _ensure_previewable_original_item(documentId, item)
    asset = _load_original_asset(item)
    if is_hwpx_source:
        try:
            return _build_generated_html_preview_response(documentId, item)
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            error = detail.get("error") if isinstance(detail.get("error"), dict) else {}
            if exc.status_code != 404 or error.get("code") != "FILE_MISSING":
                raise
            if asset is None:
                raise
            preview_name = str(item.get("originalFilename") or asset["filename"])
            media_type = resolve_preview_media_type(Path(preview_name))
            if media_type is None:
                raise
            return _build_document_bytes_response(
                content=bytes(asset["content"]),
                filename=preview_name,
                media_type=media_type,
                content_disposition_type="inline",
            )

    if asset is not None:
        preview_name = str(item.get("originalFilename") or asset["filename"])
        media_type = resolve_preview_media_type(Path(preview_name))
        if media_type is None:
            if supports_generated_html_preview(
                str(item.get("sourceFilename") or preview_name)
            ):
                return _build_generated_html_preview_response(documentId, item)
            fail(
                ErrorCode.VALIDATION_ERROR,
                f"inline preview is not supported for this file type: {Path(preview_name).suffix.lower()}",
                status=422,
                details={
                    "documentId": documentId,
                    "fileType": item.get("fileType"),
                    "previewKind": detect_document_preview_kind(
                        preview_name,
                        str(item.get("sourceFilename") or preview_name),
                    ),
                },
            )
        return _build_document_bytes_response(
            content=bytes(asset["content"]),
            filename=preview_name,
            media_type=media_type,
            content_disposition_type="inline",
        )

    file_path = _resolve_legacy_file_path(
        documentId,
        item,
        field_name="originalFilePath",
        filename_field="originalFilename",
    )
    media_type = resolve_preview_media_type(file_path)
    if media_type is None:
        if supports_generated_html_preview(file_path):
            return _build_generated_html_preview_response(documentId, item)
        fail(
            ErrorCode.VALIDATION_ERROR,
            f"inline preview is not supported for this file type: {file_path.suffix.lower()}",
            status=422,
            details={
                "documentId": documentId,
                "fileType": item.get("fileType"),
                "previewKind": detect_preview_kind(file_path),
            },
        )
    return _build_document_file_response(
        file_path,
        item,
        media_type=media_type,
        content_disposition_type="inline",
    )


@router.get(
    "/{documentId}/original",
    summary="문서 원본 미리보기",
    description="브라우저에서 바로 렌더링 가능한 원본 파일만 inline 형태로 반환합니다. PDF, 이미지, 텍스트 파일 미리보기에 사용합니다.",
)
def preview_document_original(request: Request, documentId: str):
    response = _preview_document_original_file(documentId, request)
    record_audit_event(
        action="DOCUMENT_PREVIEWED",
        resource_type="document",
        resource_id=documentId,
        request=request,
    )
    return response


def _get_document_original_content_payload(
    documentId: str, request: Request | None = None
):
    item = _get_document_item(documentId, request)

    try:
        with materialize_record_asset(
            item,
            file_id_key="sourceFileId",
            filename_key="sourceFilename",
            fallback_path_key="sourceFilePath",
            tmp_root=config.tmp_root,
            purpose="original_content",
            owner_id=documentId,
        ) as file_path:
            content, extraction_method = extract_original_document_content(file_path)
    except FileNotFoundError as exc:
        fail(
            "FILE_MISSING",
            f"original file is missing: {documentId}",
            status=404,
            details={
                "documentId": documentId,
                "storedPath": str(
                    item.get("sourceFilePath") or item.get("originalFilePath") or ""
                ),
                "reason": str(exc),
            },
        )
    except Exception as exc:  # noqa: BLE001
        fail(
            ErrorCode.VALIDATION_ERROR,
            str(exc),
            status=422,
            details={
                "documentId": documentId,
                "filePath": str(
                    item.get("sourceFilePath") or item.get("originalFilePath") or ""
                ),
            },
        )

    return ok(
        {
            "documentId": documentId,
            "status": item.get("latestStatus"),
            "fileName": item.get("sourceFilename") or item.get("originalFilename"),
            "fileType": item.get("sourceFileType") or item.get("fileType"),
            "originalFilePath": str(
                item.get("sourceFilePath") or item.get("originalFilePath") or ""
            ),
            "content": content,
            "contentLength": len(content),
            "extractionMethod": extraction_method,
        }
    )


@router.get(
    "/{documentId}/content",
    summary="문서 원문 텍스트 조회",
    description="업로드한 원본 파일 내부의 텍스트 내용을 documentId 기준으로 JSON 형태로 반환합니다. 원문 비교와 텍스트 추출 확인에 사용합니다.",
)
def get_document_original_content(request: Request, documentId: str):
    response = _get_document_original_content_payload(documentId, request)
    record_audit_event(
        action="DOCUMENT_CONTENT_READ",
        resource_type="document",
        resource_id=documentId,
        request=request,
    )
    return response


@router.get(
    "/types",
    summary="지원 파일 형식 조회",
    description="현재 문서 처리 파이프라인이 지원하는 파일 확장자 목록을 반환합니다. 업로드 컴포넌트 검증이나 안내 문구에 사용합니다.",
)
def get_supported_file_types():
    return ok({"types": [file_type.lstrip(".") for file_type in SUPPORTED_TYPES]})


@router.delete(
    "/{documentId}",
    summary="문서 삭제",
    description="저장된 문서 메타데이터와 원본 파일을 함께 삭제합니다. 문서 관리 화면의 삭제 액션에 연결할 수 있습니다.",
)
def delete_document(request: Request, documentId: str):
    item = _get_document_item(documentId, request)
    disposal = dispose_document_record(documentId, item)
    record_audit_event(
        action="DOCUMENT_DISPOSED",
        resource_type="document",
        resource_id=documentId,
        request=request,
        details={
            "originalFilename": item.get("originalFilename"),
            "fileExisted": disposal["fileExisted"],
            "deletedFileCount": len(disposal["deletedFileIds"]),
            "purgedCacheEntries": disposal["purgedCacheEntries"],
        },
    )
    return ok(
        {
            "ok": True,
            "documentId": documentId,
            "originalFilename": item.get("originalFilename"),
            "fileExisted": disposal["fileExisted"],
            "purgedCacheEntries": disposal["purgedCacheEntries"],
            "deletedAt": now_iso(),
        }
    )


@router.get(
    "/{documentId}/result",
    summary="문서 결과 조회 별칭",
    description="`/parser/documents/{documentId}/result`와 동일하게 문서 전체 텍스트와 메타데이터를 반환합니다. 문서 중심 경로로 접근할 때 사용합니다.",
)
def get_document_result_alias(request: Request, documentId: str):
    return get_document_result(request=request, documentId=documentId)

from __future__ import annotations

from pathlib import Path
from typing import Any

from api.common import now_iso
from core.documents.records import build_document_record, normalize_filename
from core.documents.storage import prepare_document_assets
from infra.storage.file_assets import store_binary_asset


def prepare_uploaded_document_assets(
    *,
    document_id: str,
    filename: str,
    content: bytes,
    tmp_root: Path | None = None,
    convert_preview: bool = True,
) -> dict[str, Any]:
    source_filename = normalize_filename(filename)
    if convert_preview:
        if tmp_root is None:
            raise ValueError("tmp_root is required when convert_preview=True")
        return prepare_document_assets(
            document_id=document_id,
            filename=source_filename,
            content=content,
            tmp_root=tmp_root,
        )

    source_asset = store_binary_asset(
        category="document_source",
        filename=source_filename,
        content=content,
        document_id=document_id,
    )
    return {
        "sourceFileId": source_asset["fileId"],
        "sourceFilename": source_asset["filename"],
        "sourceFileType": source_asset["fileType"],
        "sourceFilePath": source_asset["storagePath"],
        "originalFileId": source_asset["fileId"],
        "originalFilename": source_asset["filename"],
        "fileType": source_asset["fileType"],
        "originalFilePath": source_asset["storagePath"],
        "originalStorageKind": "source",
    }


def build_document_record_from_assets(
    *,
    document_id: str,
    assets: dict[str, Any],
    status: str,
    uploaded_at: str | None = None,
    updated_at: str | None = None,
    job_id: str | None = None,
    job_item_id: str | None = None,
    processing_time_ms: int | None = None,
    model_code: str | None = None,
    execution_backend: str | None = None,
    file_sha256: str | None = None,
    cache_key: str | None = None,
    cache_expires_at: str | None = None,
    meta: dict[str, Any] | None = None,
    output_path: str = "",
    output_file_id: str | None = None,
    output_filename: str | None = None,
    owner_user_id: str | None = None,
    error: Any = None,
) -> dict[str, Any]:
    return build_document_record(
        document_id,
        str(assets["originalFilename"]),
        status=status,
        original_file_path=str(assets["originalFilePath"]),
        original_file_id=str(assets["originalFileId"]),
        original_storage_kind=str(assets["originalStorageKind"]),
        source_filename=str(assets["sourceFilename"]),
        source_file_path=str(assets["sourceFilePath"]),
        source_file_id=str(assets["sourceFileId"]),
        uploaded_at=uploaded_at or now_iso(),
        updated_at=updated_at,
        job_id=job_id,
        job_item_id=job_item_id,
        processing_time_ms=processing_time_ms,
        model_code=model_code,
        execution_backend=execution_backend,
        file_sha256=file_sha256,
        cache_key=cache_key,
        cache_expires_at=cache_expires_at,
        meta=meta,
        output_path=output_path,
        output_file_id=output_file_id,
        output_filename=output_filename,
        owner_user_id=owner_user_id,
        error=error,
    )


def create_uploaded_document(
    *,
    document_id: str,
    filename: str,
    content: bytes,
    status: str,
    tmp_root: Path | None = None,
    convert_preview: bool = True,
    owner_user_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    assets = prepare_uploaded_document_assets(
        document_id=document_id,
        filename=filename,
        content=content,
        tmp_root=tmp_root,
        convert_preview=convert_preview,
    )
    record = build_document_record_from_assets(
        document_id=document_id,
        assets=assets,
        status=status,
        owner_user_id=owner_user_id,
    )
    return record, assets

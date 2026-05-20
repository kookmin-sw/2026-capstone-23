from __future__ import annotations

from pathlib import Path
from typing import Any

from core.time import now_iso

_PROGRESS_PERCENT_BY_STATUS = {
    "UPLOADED": 0,
    "QUEUED": 0,
    "PROCESSING": 5,
    "PREPROCESSING": 10,
    "GPU_WAITING": 20,
    "GPU_PROCESSING": 25,
    "POSTPROCESSING": 90,
    "COMPLETED": 100,
    "FAILED": 100,
    "CANCELED": 100,
}


def normalize_filename(filename: str) -> str:
    normalized = Path(filename).name.strip()
    return normalized or "unnamed"


def pick_storage_path(root: Path, filename: str, *, duplicate_policy: str = "OVERWRITE") -> Path:
    normalized = normalize_filename(filename)
    candidate = root / normalized
    if duplicate_policy != "KEEP_BOTH" or not candidate.exists():
        return candidate

    suffix = "".join(candidate.suffixes)
    stem = candidate.name[: -len(suffix)] if suffix else candidate.name
    counter = 1
    while True:
        alt = root / f"{stem}_{counter}{suffix}"
        if not alt.exists():
            return alt
        counter += 1


def pick_storage_name(filename: str, *, duplicate_policy: str = "OVERWRITE", existing_names: set[str] | None = None) -> str:
    normalized = normalize_filename(filename)
    known_names = {normalize_filename(name) for name in (existing_names or set())}
    if duplicate_policy != "KEEP_BOTH" or normalized not in known_names:
        return normalized

    suffix = "".join(Path(normalized).suffixes)
    stem = normalized[: -len(suffix)] if suffix else normalized
    counter = 1
    while True:
        candidate = f"{stem}_{counter}{suffix}"
        if candidate not in known_names:
            return candidate
        counter += 1


def build_document_record(
    document_id: str,
    original_filename: str,
    *,
    status: str,
    original_file_path: str = "",
    original_file_id: str | None = None,
    original_storage_kind: str | None = None,
    source_filename: str | None = None,
    source_file_path: str = "",
    source_file_id: str | None = None,
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
    timestamp = uploaded_at or now_iso()
    normalized_original = normalize_filename(original_filename)
    normalized_source = normalize_filename(source_filename or normalized_original)
    return {
        "documentId": document_id,
        "ownerUserId": owner_user_id,
        "title": Path(normalized_source).stem,
        "sourceFilename": normalized_source,
        "sourceFileType": Path(normalized_source).suffix.lower().lstrip(".") or "unknown",
        "sourceFilePath": source_file_path or original_file_path,
        "sourceFileId": source_file_id,
        "originalFilename": normalized_original,
        "originalFilePath": original_file_path,
        "originalFileId": original_file_id or source_file_id,
        "originalStorageKind": original_storage_kind or "source",
        "fileType": Path(normalized_original).suffix.lower().lstrip(".") or "unknown",
        "uploadedAt": timestamp,
        "updatedAt": updated_at or timestamp,
        "latestStatus": status,
        "jobId": job_id,
        "jobItemId": job_item_id,
        "processingTimeMs": processing_time_ms,
        "modelCode": model_code,
        "executionBackend": execution_backend,
        "fileSha256": file_sha256,
        "cacheKey": cache_key,
        "cacheExpiresAt": cache_expires_at,
        "meta": meta or {},
        "outputPath": output_path,
        "outputFileId": output_file_id,
        "outputFilename": normalize_filename(output_filename or f"{Path(normalized_source).stem}.txt"),
        "error": error,
    }


def document_record_for_display(record: dict[str, Any]) -> dict[str, Any]:
    display = dict(record)
    source_filename = str(display.get("sourceFilename") or "")
    if source_filename:
        display["originalFilename"] = source_filename
        display["fileType"] = (
            str(display.get("sourceFileType") or "")
            or Path(source_filename).suffix.lower().lstrip(".")
            or str(display.get("fileType") or "")
        )
    meta = dict(display.get("meta") or {})
    total_pages = _coerce_int(meta.get("totalPages") or meta.get("pageCount"))
    processed_pages = _coerce_int(meta.get("processedPages"))
    if total_pages and processed_pages is not None and total_pages > 0:
        progress_percent = int((processed_pages / total_pages) * 100)
    else:
        progress_percent = _coerce_int(meta.get("progressPercent"))
        if progress_percent is None:
            progress_percent = _PROGRESS_PERCENT_BY_STATUS.get(
                str(display.get("latestStatus") or "UPLOADED"),
                0,
            )
    display["progressPercent"] = min(100, max(0, int(progress_percent)))
    display["currentPage"] = processed_pages
    display["totalPages"] = total_pages
    return display


def _coerce_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None

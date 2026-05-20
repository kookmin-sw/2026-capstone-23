import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from api.common import ErrorCode, fail, now_iso
from core.jobs.execution import (
    AUTO_BACKEND,
    SUPPORTED_EXECUTION_BACKENDS,
    normalize_execution_backend,
    supported_execution_backends_for_model,
)
from api.ws_hub import publish_job_progress
from core.env import env_int
from core.version import PUBLIC_API_PREFIX
from infra.store import DOCUMENTS, DOCUMENT_CACHE, JOBS


PROGRESS_PERCENT_BY_STATUS = {
    "UPLOADED": 0,
    "QUEUED": 0,
    "PROCESSING": 50,
    "COMPLETED": 100,
    "FAILED": 0,
    "CANCELED": 0,
}


def sync_convert_percent(job: Dict[str, Any]) -> int:
    total = int(job.get("totalDocuments", 0) or 0)
    if total <= 0:
        return 0
    completed = int(job.get("completedDocuments", 0) or 0)
    failed = int(job.get("failedDocuments", 0) or 0)
    canceled = int(job.get("canceledDocuments", 0) or 0)
    return min(100, max(0, int(((completed + failed + canceled) / total) * 100)))


def coerce_progress_numbers(args: tuple[Any, ...]) -> tuple[int, int]:
    if len(args) >= 3:
        current, total = args[1], args[2]
    elif len(args) >= 2:
        current, total = args[0], args[1]
    else:
        return 0, 0

    current_int = coerce_int(current) or 0
    total_int = coerce_int(total) or 0
    return current_int, total_int


def percent(current: int, total: int) -> int:
    if total <= 0:
        return 0
    return min(100, max(0, int((current / total) * 100)))


def publish_sync_convert_progress(
    *,
    job_id: str,
    document_id: Optional[str],
    status: str,
    event_type: str,
    message: str,
    document_percent: Optional[int] = None,
    current_page: Optional[int] = None,
    total_pages: Optional[int] = None,
    error: Any = None,
    publisher: Callable[[dict], None] = publish_job_progress,
) -> None:
    job = JOBS.get(job_id, {})
    completed = int(job.get("completedDocuments", 0) or 0)
    failed = int(job.get("failedDocuments", 0) or 0)
    canceled = int(job.get("canceledDocuments", 0) or 0)
    total = int(job.get("totalDocuments", 0) or 0)
    job_percent = sync_convert_percent(job)
    publisher(
        {
            "type": "job.item.progress",
            "jobId": job_id,
            "jobItemId": None,
            "documentId": document_id,
            "status": status,
            "eventType": event_type,
            "percent": job_percent if document_percent is None else document_percent,
            "jobPercent": job_percent,
            "documentPercent": document_percent,
            "currentPage": current_page,
            "totalPages": total_pages,
            "totalDocuments": total,
            "completedDocuments": completed,
            "failedDocuments": failed,
            "canceledDocuments": canceled,
            "finishedDocuments": completed + failed + canceled,
            "message": message,
            "error": error,
            "timestamp": now_iso(),
        }
    )


def load_output_meta(output_path: str) -> Dict[str, Any]:
    if not output_path:
        return {}

    meta_path = Path(output_path).with_suffix(".meta.json")
    if not meta_path.exists():
        return {}

    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def load_output_meta_with_doc(doc: dict) -> Dict[str, Any]:
    meta = dict(doc.get("meta") or {})
    meta.update(load_output_meta(str(doc.get("outputPath") or "")))
    return meta


def coerce_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def cache_ttl_seconds() -> int:
    return env_int("DOCUMENT_CACHE_TTL_SECONDS", 3600, minimum=0)


def iso_from_dt(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso8601(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def cache_key(
    file_sha256: str,
    model_code: str,
    language: str,
    *,
    owner_user_id: str | None = None,
) -> str:
    owner_part = owner_user_id or ""
    key_text = f"{file_sha256}:{model_code}:{language}:{owner_part}"
    return hashlib.sha256(key_text.encode("utf-8")).hexdigest()


def cache_expiry(ttl_seconds: int) -> Optional[str]:
    if ttl_seconds <= 0:
        return None
    return iso_from_dt(datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds))


def get_cached_document(cache_key_value: str, *, owner_user_id: str | None = None) -> Optional[dict]:
    entry = DOCUMENT_CACHE.get(cache_key_value)
    if not entry:
        return None

    expires_at = parse_iso8601(entry.get("expiresAt"))
    if expires_at and expires_at <= datetime.now(timezone.utc):
        DOCUMENT_CACHE.pop(cache_key_value, None)
        return None

    doc = DOCUMENTS.get(str(entry.get("documentId") or ""))
    if not doc or doc.get("latestStatus") != "COMPLETED":
        return None
    if owner_user_id and str(doc.get("ownerUserId") or "") != owner_user_id:
        return None

    output_file_id = str(doc.get("outputFileId") or "")
    output_path = Path(doc.get("outputPath", "")) if doc.get("outputPath") else None
    if not output_file_id and (not output_path or not output_path.exists()):
        return None

    return doc


def result_url(document_id: str) -> str:
    return f"{PUBLIC_API_PREFIX}/parser/documents/{document_id}/result"


def build_convert_item(
    *,
    document_id: str,
    original_name: str,
    original_file_path: str,
    output_txt_path: str,
    elapsed_ms: int,
    status: str,
    error_info: Any,
    cache_hit: bool = False,
) -> Dict[str, Any]:
    return {
        "documentId": document_id,
        "fileName": original_name,
        "originalFilePath": original_file_path,
        "txt": {
            "artifactId": f"a_txt_{uuid.uuid4().hex[:8]}",
            "path": output_txt_path,
            "preview": "",
        },
        "resultUrl": result_url(document_id),
        "htmlPreview": None,
        "markdown": None,
        "imageDescriptions": [],
        "meta": {
            "totalPages": None,
            "processedPages": None,
            "processingTimeMs": elapsed_ms,
            "completedAt": now_iso(),
            "cacheHit": cache_hit,
        },
        "status": status,
        "error": error_info,
        "cacheHit": cache_hit,
        "deduplicated": cache_hit,
    }


def validate_execution_backend(value: str) -> str:
    normalized = normalize_execution_backend(value, default=AUTO_BACKEND)
    raw = (value or AUTO_BACKEND).strip().lower()
    if raw not in SUPPORTED_EXECUTION_BACKENDS:
        fail(
            ErrorCode.VALIDATION_ERROR,
            f"executionBackend must be one of: {', '.join(sorted(SUPPORTED_EXECUTION_BACKENDS))}",
            status=422,
        )
    return normalized


def validate_execution_backend_for_model(*, requested_backend: str, model_id: str, model_code: str) -> None:
    if requested_backend == AUTO_BACKEND:
        return

    supported = supported_execution_backends_for_model(model_id=model_id, model_code=model_code)
    if requested_backend in supported:
        return

    fail(
        ErrorCode.VALIDATION_ERROR,
        (
            f"executionBackend={requested_backend} is not supported for modelId={model_id}; "
            f"allowed backends: {', '.join(sorted(supported))}"
        ),
        status=422,
    )


def resolve_item_timeout_seconds(*, execution_backend: str, model_code: str) -> int:
    if execution_backend == "qwen_gpu":
        if model_code.strip().lower().startswith("qwen2.5-vl"):
            return max(300, env_int("QWEN_WORKER_LEASE_TTL_SECONDS", 3600))
        return max(300, env_int("LEGACY_GPU_ITEM_TIMEOUT_SECONDS", 900))
    return max(60, env_int("JOB_ITEM_TIMEOUT_SECONDS", 300))


def resolve_item_max_retries(*, execution_backend: str, model_code: str) -> int:
    if execution_backend == "qwen_gpu" and model_code.strip().lower().startswith("qwen2.5-vl"):
        return max(0, env_int("QWEN_ITEM_MAX_RETRIES", 3))
    return max(0, env_int("JOB_ITEM_MAX_RETRIES", 3))

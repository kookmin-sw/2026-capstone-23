from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Any, Dict, List, Optional

from core.env import env_int
from core.time import now_iso
from infra.queue.job_queue import dequeue, dequeue_message, enqueue as enqueue_queue
from infra.queue.message import QueueMessage
from infra.progress_events import publish_progress_event
from infra.store import (
    DOCUMENTS,
    JOB_EVENTS,
    JOB_ITEMS,
    JOBS,
    QWEN_FINALIZE_TASKS,
    QWEN_INFER_RESULTS,
    QWEN_INFER_TASKS,
    WORKER_LEASES,
)

FINAL_STATES = {"COMPLETED", "FAILED", "CANCELED"}
PROCESSING_STAGES = {"PROCESSING", "PREPROCESSING", "GPU_WAITING", "GPU_PROCESSING", "POSTPROCESSING"}
# 실행 워커/복구 워커가 동시에 상태를 만질 수 있어 전역 상태 락으로 직렬화한다.
_STATE_LOCK = RLock()

_EVENT_TYPES = {"CREATED", "STARTED", "RETRIED", "COMPLETED", "FAILED", "CANCELED"}
_DEFAULT_EVENT_RETENTION_SECONDS = 7 * 24 * 60 * 60
_progress_publisher: Callable[[dict[str, Any]], None] | None = None

_STAGE_PROGRESS_FLOORS = {
    "QUEUED": 0,
    "PROCESSING": 5,
    "PREPROCESSING": 10,
    "GPU_WAITING": 20,
    "GPU_PROCESSING": 25,
    "POSTPROCESSING": 90,
}
_STAGE_ORDER = {
    "QUEUED": 0,
    "PROCESSING": 1,
    "PREPROCESSING": 2,
    "GPU_WAITING": 3,
    "GPU_PROCESSING": 4,
    "POSTPROCESSING": 5,
}


def set_job_progress_publisher(publisher: Callable[[dict[str, Any]], None] | None) -> None:
    global _progress_publisher
    _progress_publisher = publisher


def _publish_job_progress(payload: dict[str, Any]) -> None:
    if _progress_publisher is not None:
        _progress_publisher(payload)
    publish_progress_event(payload)


def _bounded_percent(value: Any, default: int = 0) -> int:
    try:
        percent = int(value)
    except (TypeError, ValueError):
        percent = default
    return min(100, max(0, percent))


def _item_progress_percent(item: Dict[str, Any]) -> int:
    status = str(item.get("status") or "")
    if status in FINAL_STATES:
        return 100
    if item.get("progressPercent") is not None:
        return _bounded_percent(item.get("progressPercent"))
    stage = str(item.get("stage") or status or "QUEUED")
    return _STAGE_PROGRESS_FLOORS.get(stage, 0)


def _job_progress_percent_from_items(items: list[Dict[str, Any]], total: int) -> int:
    if total <= 0:
        return 0
    completed_units = sum(_item_progress_percent(item) for item in items)
    return _bounded_percent(completed_units / total)


def _sync_job_progress(job: Dict[str, Any]) -> int:
    job_id = str(job.get("jobId") or "")
    items = [item for item in JOB_ITEMS.values() if item.get("jobId") == job_id]
    total = int(job.get("totalItems", len(items)) or len(items))
    progress = _job_progress_percent_from_items(items, total)
    if str(job.get("status") or "") in FINAL_STATES:
        progress = 100
    job["progressPercent"] = progress
    return progress


def _set_item_progress(
    item: Dict[str, Any],
    *,
    progress_percent: int,
    current_page: Optional[int] = None,
    total_pages: Optional[int] = None,
) -> int:
    progress = _bounded_percent(progress_percent)
    item["progressPercent"] = progress
    if current_page is not None:
        item["currentPage"] = max(0, int(current_page))
    if total_pages is not None:
        item["totalPages"] = max(0, int(total_pages))
    return progress


def _job_parallelism_limit(job: Dict[str, Any]) -> int:
    try:
        return max(1, int(job.get("parallelism", 1)))
    except (TypeError, ValueError):
        return 1


def _is_staged_qwen_item(item: Dict[str, Any]) -> bool:
    model_code = str(item.get("modelCode") or "").strip().lower()
    return (
        str(item.get("executionBackend") or "") == "qwen_gpu"
        and str(item.get("queueRoute") or "") == "qwen_doc"
        and model_code.startswith("qwen2.5-vl")
    )


def _counts_against_claim_parallelism(
    active_item: Dict[str, Any],
    *,
    claim_item: Dict[str, Any],
    claim_stage: str,
) -> bool:
    if active_item.get("status") != "PROCESSING":
        return False

    if (
        claim_stage == "PREPROCESSING"
        and _is_staged_qwen_item(claim_item)
        and _is_staged_qwen_item(active_item)
    ):
        return str(active_item.get("stage") or "") == "PREPROCESSING"

    return True


def _job_processing_count(job_id: str, *, claim_item: Dict[str, Any], claim_stage: str) -> int:
    return sum(
        1
        for item in JOB_ITEMS.values()
        if item.get("jobId") == job_id
        and _counts_against_claim_parallelism(
            item,
            claim_item=claim_item,
            claim_stage=claim_stage,
        )
    )


def _append_event(
    *,
    job_id: str,
    event_type: str,
    job_item_id: Optional[str] = None,
    status: Optional[str] = None,
    worker_id: Optional[str] = None,
    retry_count: Optional[int] = None,
    error: Any = None,
) -> str:
    if event_type not in _EVENT_TYPES:
        raise ValueError(f"unsupported event_type: {event_type}")

    event_id = f"e_{uuid.uuid4().hex[:12]}"
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    JOB_EVENTS[event_id] = {
        "eventId": event_id,
        "jobId": job_id,
        "jobItemId": job_item_id,
        "eventType": event_type,
        "status": status,
        "workerId": worker_id,
        "retryCount": retry_count,
        "error": error,
        "createdAt": now,
    }
    item = JOB_ITEMS.get(job_item_id) if job_item_id else None
    job = JOBS.get(job_id) or {}
    total_items = int(job.get("totalItems", 0) or 0)
    completed_items = int(job.get("completedItems", 0) or 0)
    failed_items = int(job.get("failedItems", 0) or 0)
    canceled_items = int(job.get("canceledItems", 0) or 0)
    finished_items = completed_items + failed_items + canceled_items
    job_percent = _sync_job_progress(job) if job else 0
    document_percent = _item_progress_percent(item) if item else None
    _publish_job_progress(
        {
            "type": "job.item.progress",
            "jobId": job_id,
            "jobItemId": job_item_id,
            "documentId": item.get("documentId") if item else None,
            "status": status,
            "eventType": event_type,
            "percent": job_percent,
            "jobPercent": job_percent,
            "documentPercent": document_percent,
            "progressPercent": document_percent if item else job_percent,
            "currentPage": item.get("currentPage") if item else None,
            "totalPages": item.get("totalPages") if item else None,
            "totalItems": total_items,
            "completedItems": completed_items,
            "failedItems": failed_items,
            "canceledItems": canceled_items,
            "finishedItems": finished_items,
            "workerId": worker_id,
            "retryCount": retry_count,
            "error": error,
            "timestamp": now,
        }
    )
    return event_id


def create_job(
    *,
    model_id: str,
    parallelism: int,
    total_items: int,
    requested_execution_backend: str,
    execution_backend: str,
    owner_user_id: str | None = None,
) -> str:
    job_id = f"j_{uuid.uuid4().hex[:10]}"
    now = now_iso()
    with _STATE_LOCK:
        JOBS[job_id] = {
            "jobId": job_id,
            "ownerUserId": owner_user_id,
            "status": "QUEUED",
            "modelId": model_id,
            "parallelism": parallelism,
            "requestedExecutionBackend": requested_execution_backend,
            "executionBackend": execution_backend,
            "cancelRequested": False,
            "totalItems": total_items,
            "queuedItems": total_items,
            "processingItems": 0,
            "completedItems": 0,
            "failedItems": 0,
            "canceledItems": 0,
            "progressPercent": 0,
            "queuedAt": now,
            "startedAt": None,
            "completedAt": None,
            "updatedAt": now,
        }
        _append_event(job_id=job_id, event_type="CREATED", status="QUEUED")
    return job_id


def create_job_item(
    *,
    job_id: str,
    document_id: str,
    file_name: str,
    source_path: str,
    source_file_id: str | None = None,
    source_filename: str | None = None,
    language: str,
    execution_backend: str,
    queue_route: str,
    model_code: str | None = None,
    timeout_seconds: int | None = None,
    max_retries: int = 3,
    owner_user_id: str | None = None,
) -> str:
    item_id = f"ji_{uuid.uuid4().hex[:12]}"
    now = now_iso()
    with _STATE_LOCK:
        JOB_ITEMS[item_id] = {
            "jobItemId": item_id,
            "jobId": job_id,
            "documentId": document_id,
            "ownerUserId": owner_user_id,
            "fileName": file_name,
            "sourcePath": source_path,
            "sourceFileId": source_file_id,
            "sourceFilename": source_filename or file_name,
            "language": language,
            "modelCode": model_code,
            "executionBackend": execution_backend,
            "queueRoute": queue_route,
            "status": "QUEUED",
            "stage": "QUEUED",
            "retryCount": 0,
            "maxRetries": max_retries,
            "timeoutSeconds": timeout_seconds,
            "lastError": None,
            "workerId": None,
            "progressPercent": 0,
            "currentPage": None,
            "totalPages": None,
            "firstQueuedAt": now,
            "queuedAt": now,
            "startedAt": None,
            "stageStartedAt": now,
            "completedAt": None,
            "updatedAt": now,
            "outputPath": "",
            "outputFileId": None,
        }
        _append_event(job_id=job_id, job_item_id=item_id, event_type="CREATED", status="QUEUED")
    try:
        enqueue_queue(item_id, job_id=job_id, attempt=0, queued_at=now, queue_route=queue_route)
    except Exception as exc:
        failed_at = now_iso()
        with _STATE_LOCK:
            item = JOB_ITEMS.get(item_id)
            if item is not None:
                item["status"] = "FAILED"
                item["stage"] = "FAILED"
                item["lastError"] = {
                    "code": "QUEUE_UNAVAILABLE",
                    "message": str(exc),
                }
                item["completedAt"] = failed_at
                item["updatedAt"] = failed_at
                _append_event(
                    job_id=job_id,
                    job_item_id=item_id,
                    event_type="FAILED",
                    status="FAILED",
                    error=item["lastError"],
                )
            job = JOBS.get(job_id)
            if job is not None:
                _recompute_job_status(job)
        raise
    return item_id


def _sync_document_with_item(doc: Dict[str, Any], item: Dict[str, Any], *, item_id: str, status: str, now: str) -> None:
    doc["jobId"] = item["jobId"]
    doc["jobItemId"] = item_id
    if not doc.get("ownerUserId"):
        owner_user_id = item.get("ownerUserId")
        if not owner_user_id:
            job = JOBS.get(str(item.get("jobId") or ""))
            owner_user_id = (job or {}).get("ownerUserId")
        if owner_user_id:
            doc["ownerUserId"] = owner_user_id
    doc["sourceFilePath"] = item.get("sourcePath", doc.get("sourceFilePath", ""))
    if item.get("sourceFileId"):
        doc["sourceFileId"] = item.get("sourceFileId")
    if item.get("sourceFilename"):
        doc["sourceFilename"] = item.get("sourceFilename")
    if not doc.get("originalFilePath"):
        doc["originalFilePath"] = item.get("sourcePath", doc.get("originalFilePath", ""))
    if not doc.get("originalFileId") and item.get("sourceFileId"):
        doc["originalFileId"] = item.get("sourceFileId")
    if not doc.get("originalFilename") and item.get("sourceFilename"):
        doc["originalFilename"] = item.get("sourceFilename")
    doc["latestStatus"] = status
    doc["executionBackend"] = item.get("executionBackend", doc.get("executionBackend"))
    doc["updatedAt"] = now


def mark_item_processing(item_id: str, worker_id: str, stage: str = "PROCESSING") -> bool:
    # QUEUED -> PROCESSING 전이를 원자적으로 수행한다.
    with _STATE_LOCK:
        item = JOB_ITEMS.get(item_id)
        if not item:
            return False

        current = item.get("status")
        if current != "QUEUED":
            return False

        job = JOBS.get(item["jobId"])
        if not job:
            return False

        if job.get("cancelRequested"):
            return False

        if (
            _job_processing_count(item["jobId"], claim_item=item, claim_stage=stage)
            >= _job_parallelism_limit(job)
        ):
            return False

        now = now_iso()
        item["status"] = "PROCESSING"
        item["stage"] = stage
        item["workerId"] = worker_id
        # retry 시점 포함해 매번 시작시각을 현재로 갱신
        item["startedAt"] = now
        item["stageStartedAt"] = now
        item["updatedAt"] = now
        _set_item_progress(
            item,
            progress_percent=max(
                _item_progress_percent(item),
                _STAGE_PROGRESS_FLOORS.get(stage, 5),
            ),
        )

        doc = DOCUMENTS.get(item["documentId"])
        if doc is not None:
            _sync_document_with_item(doc, item, item_id=item_id, status=stage, now=now)
            meta = dict(doc.get("meta") or {})
            meta["progressPercent"] = item["progressPercent"]
            doc["meta"] = meta

        if job.get("startedAt") is None:
            job["startedAt"] = now
        _recompute_job_status(job)
        _append_event(
            job_id=item["jobId"],
            job_item_id=item_id,
            event_type="STARTED",
            status=stage,
            worker_id=worker_id,
        )
        return True


def mark_item_stage(item_id: str, stage: str, worker_id: Optional[str] = None, error: Any = None) -> bool:
    with _STATE_LOCK:
        item = JOB_ITEMS.get(item_id)
        if not item:
            return False

        if item.get("status") not in {"PROCESSING", "QUEUED"}:
            return False

        job = JOBS.get(item["jobId"])
        if not job:
            return False

        now = now_iso()
        previous_stage = str(item.get("stage") or "")
        if item.get("status") == "QUEUED":
            item["status"] = "PROCESSING"
            if item.get("startedAt") is None:
                item["startedAt"] = now
        item["stage"] = stage
        if previous_stage != stage or not item.get("stageStartedAt"):
            item["stageStartedAt"] = now
        item["updatedAt"] = now
        if worker_id is not None:
            item["workerId"] = worker_id
        if error is not None:
            item["lastError"] = error
        _set_item_progress(
            item,
            progress_percent=max(
                _item_progress_percent(item),
                _STAGE_PROGRESS_FLOORS.get(stage, 5),
            ),
        )

        doc = DOCUMENTS.get(item["documentId"])
        if doc is not None:
            _sync_document_with_item(doc, item, item_id=item_id, status=stage, now=now)
            doc["error"] = error
            meta = dict(doc.get("meta") or {})
            meta["progressPercent"] = item["progressPercent"]
            doc["meta"] = meta

        if job.get("startedAt") is None:
            job["startedAt"] = now
        _recompute_job_status(job)
        _append_event(
            job_id=item["jobId"],
            job_item_id=item_id,
            event_type="STARTED",
            status=stage,
            worker_id=item.get("workerId"),
            error=error,
        )
        return True


def mark_item_progress(
    item_id: str,
    current: int,
    total: int,
    *,
    worker_id: Optional[str] = None,
    stage: Optional[str] = None,
    progress_percent: Optional[int] = None,
    detail: Optional[Dict[str, Any]] = None,
) -> bool:
    with _STATE_LOCK:
        item = JOB_ITEMS.get(item_id)
        if not item:
            return False
        if item.get("status") != "PROCESSING":
            return False

        job = JOBS.get(item["jobId"])
        if not job:
            return False

        now = now_iso()
        current_int = max(0, int(current or 0))
        total_int = max(0, int(total or 0))
        computed_percent = (
            progress_percent
            if progress_percent is not None
            else int((current_int / total_int) * 100)
            if total_int > 0
            else _item_progress_percent(item)
        )
        bounded_percent = _set_item_progress(
            item,
            progress_percent=max(_item_progress_percent(item), computed_percent),
            current_page=current_int,
            total_pages=total_int,
        )
        if stage is not None and _STAGE_ORDER.get(stage, 0) >= _STAGE_ORDER.get(
            str(item.get("stage") or ""),
            0,
        ):
            item["stage"] = stage
        if worker_id is not None:
            item["workerId"] = worker_id
        item["updatedAt"] = now

        doc = DOCUMENTS.get(item["documentId"])
        if doc is not None:
            _sync_document_with_item(
                doc,
                item,
                item_id=item_id,
                status=str(item.get("stage") or item.get("status") or "PROCESSING"),
                now=now,
            )
            meta = dict(doc.get("meta") or {})
            meta["progressPercent"] = bounded_percent
            meta["processedPages"] = current_int
            if total_int > 0:
                meta["totalPages"] = total_int
                meta["pageCount"] = total_int
            if detail:
                meta.update(detail)
            doc["meta"] = meta

        job_percent = _sync_job_progress(job)
        _publish_job_progress(
            {
                "type": "job.item.progress",
                "jobId": item["jobId"],
                "jobItemId": item_id,
                "documentId": item.get("documentId"),
                "status": item.get("stage") or item.get("status"),
                "eventType": "PROGRESS",
                "percent": job_percent,
                "jobPercent": job_percent,
                "documentPercent": bounded_percent,
                "progressPercent": bounded_percent,
                "currentPage": current_int,
                "totalPages": total_int,
                "totalItems": int(job.get("totalItems", 0) or 0),
                "completedItems": int(job.get("completedItems", 0) or 0),
                "failedItems": int(job.get("failedItems", 0) or 0),
                "canceledItems": int(job.get("canceledItems", 0) or 0),
                "finishedItems": (
                    int(job.get("completedItems", 0) or 0)
                    + int(job.get("failedItems", 0) or 0)
                    + int(job.get("canceledItems", 0) or 0)
                ),
                "workerId": item.get("workerId"),
                "retryCount": int(item.get("retryCount", 0) or 0),
                "error": item.get("lastError"),
                "detail": detail or {},
                "timestamp": now,
            }
        )
        return True


def mark_item_retry(item_id: str, error: Any = None) -> bool:
    # PROCESSING 상태에서만 재큐잉해 중복 retry/중복 enqueue를 방지한다.
    with _STATE_LOCK:
        item = JOB_ITEMS.get(item_id)
        if not item:
            return False

        if item.get("status") != "PROCESSING":
            return False

        job = JOBS.get(item["jobId"])
        if not job:
            return False

        now = now_iso()
        item["retryCount"] = int(item.get("retryCount", 0)) + 1
        item["status"] = "QUEUED"
        item["stage"] = "QUEUED"
        item["workerId"] = None
        item["lastError"] = error
        item["queuedAt"] = now
        # 다음 시도의 timeout 측정을 위해 startedAt 초기화
        item["startedAt"] = None
        item["stageStartedAt"] = now
        item["updatedAt"] = now
        _set_item_progress(item, progress_percent=0, current_page=0, total_pages=None)

        doc = DOCUMENTS.get(item["documentId"])
        if doc is not None:
            _sync_document_with_item(doc, item, item_id=item_id, status="QUEUED", now=now)
            doc["error"] = error
            doc["processingTimeMs"] = None
            meta = dict(doc.get("meta") or {})
            meta["progressPercent"] = 0
            meta["processedPages"] = 0
            doc["meta"] = meta

        _recompute_job_status(job)
        _append_event(
            job_id=item["jobId"],
            job_item_id=item_id,
            event_type="RETRIED",
            status="QUEUED",
            retry_count=int(item.get("retryCount", 0)),
            error=error,
        )

    enqueue_queue(
        item_id,
        job_id=item["jobId"],
        attempt=int(item.get("retryCount", 0)),
        queued_at=item.get("queuedAt"),
        queue_route=item.get("queueRoute"),
    )
    return True


def mark_item_final(
    item_id: str,
    status: str,
    output_path: str = "",
    *,
    output_file_id: str | None = None,
    meta: Dict[str, Any] | None = None,
    error: Any = None,
) -> bool:
    # 종료 전이(COMPLETED/FAILED/CANCELED)는 item당 1회만 허용한다.
    with _STATE_LOCK:
        item = JOB_ITEMS.get(item_id)
        if not item:
            return False

        # 같은 item에 대한 중복 종료 호출 방지
        if item.get("status") in FINAL_STATES:
            return False

        job = JOBS.get(item["jobId"])
        if not job:
            return False

        now = now_iso()
        item["status"] = status
        item["stage"] = status
        item["completedAt"] = now
        item["stageStartedAt"] = now
        item["updatedAt"] = now
        item["outputPath"] = output_path
        item["outputFileId"] = output_file_id
        item["lastError"] = error
        _set_item_progress(item, progress_percent=100 if status == "COMPLETED" else _item_progress_percent(item))
        processing_time_ms = _to_ms(parse_iso8601(item.get("startedAt")), parse_iso8601(item.get("completedAt")))

        doc = DOCUMENTS.get(item["documentId"])
        if doc is not None:
            _sync_document_with_item(doc, item, item_id=item_id, status=status, now=now)
            doc["outputPath"] = output_path
            doc["outputFileId"] = output_file_id
            doc["error"] = error
            doc["processingTimeMs"] = processing_time_ms
            doc_meta = dict(meta or doc.get("meta") or {})
            doc_meta["progressPercent"] = 100 if status == "COMPLETED" else item["progressPercent"]
            if item.get("currentPage") is not None:
                doc_meta["processedPages"] = item.get("currentPage")
            if item.get("totalPages") is not None:
                doc_meta["totalPages"] = item.get("totalPages")
                doc_meta["pageCount"] = item.get("totalPages")
            doc["meta"] = doc_meta

        _recompute_job_status(job)
        _append_event(
            job_id=item["jobId"],
            job_item_id=item_id,
            event_type=status,
            status=status,
            worker_id=item.get("workerId"),
            retry_count=int(item.get("retryCount", 0)),
            error=error,
        )
        return True


def request_cancel(job_id: str) -> int:
    # v1 취소 정책: 미시작(QUEUED) 항목은 즉시 CANCELED로 확정한다.
    canceled_items = 0
    with _STATE_LOCK:
        job = JOBS[job_id]
        now = now_iso()
        job["cancelRequested"] = True
        job["updatedAt"] = now

        # v1 취소 정책: 아직 시작 안 한 QUEUED item은 즉시 CANCELED
        for item in JOB_ITEMS.values():
            if item.get("jobId") != job_id:
                continue
            if item.get("status") != "QUEUED":
                continue

            item["status"] = "CANCELED"
            item["stage"] = "CANCELED"
            item["completedAt"] = now
            item["updatedAt"] = now
            item["lastError"] = {"code": "JOB_CANCELED", "message": "job canceled"}
            _set_item_progress(item, progress_percent=100)

            doc = DOCUMENTS.get(item.get("documentId"))
            if doc is not None:
                _sync_document_with_item(
                    doc,
                    item,
                    item_id=str(item.get("jobItemId") or ""),
                    status="CANCELED",
                    now=now,
                )
                doc["error"] = {"code": "JOB_CANCELED", "message": "job canceled"}
                doc["processingTimeMs"] = 0
                meta = dict(doc.get("meta") or {})
                meta["progressPercent"] = 100
                doc["meta"] = meta
            _append_event(
                job_id=job_id,
                job_item_id=item.get("jobItemId"),
                event_type="CANCELED",
                status="CANCELED",
                retry_count=int(item.get("retryCount", 0)),
                error={"code": "JOB_CANCELED", "message": "job canceled"},
            )
            canceled_items += 1

        _recompute_job_status(job)
    return canceled_items


def force_cancel(job_id: str) -> int:
    canceled_items = 0
    cancel_error = {"code": "JOB_FORCE_CANCELED", "message": "job force canceled"}
    with _STATE_LOCK:
        job = JOBS[job_id]
        now = now_iso()
        job["cancelRequested"] = True
        job["updatedAt"] = now

        target_item_ids: set[str] = set()
        for item_id, item in list(JOB_ITEMS.items()):
            if item.get("jobId") != job_id:
                continue
            target_item_ids.add(item_id)
            if item.get("status") in FINAL_STATES:
                continue

            item["status"] = "CANCELED"
            item["stage"] = "CANCELED"
            item["completedAt"] = now
            item["stageStartedAt"] = now
            item["updatedAt"] = now
            item["lastError"] = cancel_error
            _set_item_progress(item, progress_percent=100)

            doc = DOCUMENTS.get(item.get("documentId"))
            if doc is not None:
                _sync_document_with_item(doc, item, item_id=item_id, status="CANCELED", now=now)
                doc["error"] = cancel_error
                doc["processingTimeMs"] = _to_ms(parse_iso8601(item.get("startedAt")), parse_iso8601(now)) or 0
                meta = dict(doc.get("meta") or {})
                meta["progressPercent"] = 100
                doc["meta"] = meta

            _append_event(
                job_id=job_id,
                job_item_id=item_id,
                event_type="CANCELED",
                status="CANCELED",
                worker_id=item.get("workerId"),
                retry_count=int(item.get("retryCount", 0)),
                error=cancel_error,
            )
            canceled_items += 1

        for task_id, task in list(QWEN_INFER_TASKS.items()):
            if task.get("jobId") != job_id and task.get("jobItemId") not in target_item_ids:
                continue
            if str(task.get("status") or "") in FINAL_STATES:
                continue
            task["status"] = "CANCELED"
            task["completedAt"] = now
            task["updatedAt"] = now
            task["lastError"] = cancel_error
            QWEN_INFER_RESULTS.pop(task_id, None)

        for task in list(QWEN_FINALIZE_TASKS.values()):
            if task.get("jobId") != job_id and task.get("jobItemId") not in target_item_ids:
                continue
            if str(task.get("status") or "") in FINAL_STATES:
                continue
            task["status"] = "CANCELED"
            task["completedAt"] = now
            task["updatedAt"] = now
            task["lastError"] = cancel_error

        for lease_id, lease in list(WORKER_LEASES.items()):
            if lease.get("jobId") == job_id or lease.get("jobItemId") in target_item_ids:
                WORKER_LEASES.pop(lease_id, None)

        _recompute_job_status(job)
    return canceled_items


def _recompute_job_status(job: Dict[str, Any]) -> None:
    # 카운터를 증감하지 않고 item 상태를 재집계해 정합성을 유지한다.
    job_id = job["jobId"]
    items = [item for item in JOB_ITEMS.values() if item.get("jobId") == job_id]

    queued = sum(1 for i in items if i.get("status") == "QUEUED")
    processing = sum(1 for i in items if i.get("status") == "PROCESSING")
    completed = sum(1 for i in items if i.get("status") == "COMPLETED")
    failed = sum(1 for i in items if i.get("status") == "FAILED")
    canceled = sum(1 for i in items if i.get("status") == "CANCELED")

    total = int(job.get("totalItems", len(items)))
    done = completed + failed + canceled
    now = now_iso()

    job["queuedItems"] = queued
    job["processingItems"] = processing
    job["completedItems"] = completed
    job["failedItems"] = failed
    job["canceledItems"] = canceled

    if done >= total and total > 0:
        # v1 정책: 전부 취소만 된 경우만 CANCELED, 그 외 완료/실패 혼합은 COMPLETED
        if canceled == total:
            job["status"] = "CANCELED"
        elif completed == 0 and failed > 0 and canceled == 0:
            job["status"] = "FAILED"
        else:
            job["status"] = "COMPLETED"
        job["completedAt"] = now
    elif job.get("cancelRequested") and processing == 0 and queued == 0:
        job["status"] = "CANCELED"
        job["completedAt"] = now
    else:
        job["status"] = "PROCESSING" if processing > 0 else "QUEUED"

    job["progressPercent"] = _job_progress_percent_from_items(items, total)
    if job["status"] in FINAL_STATES:
        job["progressPercent"] = 100
    job["updatedAt"] = now


def list_job_items(job_id: str) -> List[Dict[str, Any]]:
    with _STATE_LOCK:
        return [item.copy() for item in JOB_ITEMS.values() if item["jobId"] == job_id]


def list_item_events(job_id: str, item_id: str, limit: int = 20) -> List[Dict[str, Any]]:
    with _STATE_LOCK:
        events = [
            event.copy()
            for event in JOB_EVENTS.values()
            if event.get("jobId") == job_id and event.get("jobItemId") == item_id
        ]
    events.sort(key=lambda e: (e.get("createdAt") or "", e.get("eventId") or ""))
    return events[-limit:]


def list_job_events(job_id: str, limit: int = 100, job_item_id: Optional[str] = None) -> List[Dict[str, Any]]:
    with _STATE_LOCK:
        events = [
            event.copy()
            for event in JOB_EVENTS.values()
            if event.get("jobId") == job_id and (job_item_id is None or event.get("jobItemId") == job_item_id)
        ]
    events.sort(key=lambda e: (e.get("createdAt") or "", e.get("eventId") or ""))
    return events[-limit:]


def cleanup_old_job_events(
    now: Optional[datetime] = None,
    retention_seconds: Optional[int] = None,
) -> int:
    if retention_seconds is None:
        retention_seconds = env_int("JOB_EVENTS_RETENTION_SECONDS", _DEFAULT_EVENT_RETENTION_SECONDS)
    if retention_seconds <= 0:
        return 0

    now_dt = now or utc_now()
    cutoff = now_dt - timedelta(seconds=retention_seconds)
    removed = 0

    with _STATE_LOCK:
        for event_id, event in list(JOB_EVENTS.items()):
            created_at = parse_iso8601(event.get("createdAt"))
            if not created_at or created_at > cutoff:
                continue

            job_id = event.get("jobId")
            job = JOBS.get(str(job_id)) if job_id else None
            if job is None:
                JOB_EVENTS.pop(event_id, None)
                removed += 1
                continue

            status = str(job.get("status") or "")
            if status not in FINAL_STATES:
                continue

            completed_at = parse_iso8601(job.get("completedAt"))
            if completed_at and completed_at <= cutoff:
                JOB_EVENTS.pop(event_id, None)
                removed += 1

    return removed


def next_item(queue_route: Optional[str] = None) -> Optional[str]:
    return dequeue(queue_route=queue_route)


def next_item_message(queue_route: Optional[str] = None) -> Optional[QueueMessage]:
    return dequeue_message(queue_route=queue_route)


def _to_ms(start: Optional[datetime], end: Optional[datetime]) -> Optional[int]:
    if not start or not end:
        return None
    return max(0, int((end - start).total_seconds() * 1000))


def build_item_timings(item: Dict[str, Any]) -> Dict[str, Optional[int]]:
    queued_at = parse_iso8601(item.get("queuedAt"))
    first_queued_at = parse_iso8601(item.get("firstQueuedAt")) or queued_at
    started_at = parse_iso8601(item.get("startedAt"))
    completed_at = parse_iso8601(item.get("completedAt"))
    return {
        "queueWaitMs": _to_ms(queued_at, started_at),
        "processingTimeMs": _to_ms(started_at, completed_at),
        "endToEndMs": _to_ms(first_queued_at, completed_at),
    }


def parse_iso8601(iso_text: Optional[str]) -> Optional[datetime]:
    if not iso_text:
        return None
    try:
        return datetime.fromisoformat(iso_text.replace("Z", "+00:00"))
    except ValueError:
        return None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)

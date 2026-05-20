from __future__ import annotations

import os
from hashlib import sha256
from typing import Any, Literal, Optional

from fastapi import APIRouter, Query, Request

from api.common import fail, ok
from api.dependencies import get_config
from api.security import filter_records_for_user, get_request_user, is_admin_user, record_owner_id
from infra.storage.settings import disk_usage, get_configured_storage_path
from infra.store import DOCUMENTS, JOB_EVENTS, JOB_ITEMS, JOBS


router = APIRouter(prefix="/monitoring", tags=["monitoring"])


def _error_message(error: Any) -> str:
    if isinstance(error, dict):
        return str(error.get("message") or error.get("code") or "unknown error")
    return str(error or "unknown error")


def _error_code(error: Any) -> str:
    if isinstance(error, dict):
        return str(error.get("code") or "UNKNOWN_ERROR")
    return "UNKNOWN_ERROR"


def _stable_record_suffix(*values: Any) -> str:
    payload = "|".join(str(value or "") for value in values) or "unknown"
    return sha256(payload.encode("utf-8", errors="replace")).hexdigest()[:16]


def _event_error_id(event: dict[str, Any]) -> str:
    event_id = event.get("eventId")
    if event_id:
        return f"err_{event_id}"
    return "err_event_" + _stable_record_suffix(
        event.get("jobId"),
        event.get("jobItemId"),
        event.get("eventType"),
        event.get("status"),
        event.get("createdAt"),
        _error_code(event.get("error")),
        _error_message(event.get("error")),
    )


def _item_error_id(item: dict[str, Any]) -> str:
    item_id = item.get("jobItemId")
    if item_id:
        return f"err_{item_id}"
    return "err_item_" + _stable_record_suffix(
        item.get("jobId"),
        item.get("documentId"),
        item.get("fileName"),
        item.get("sourcePath"),
        item.get("status"),
        item.get("updatedAt") or item.get("completedAt"),
        _error_code(item.get("lastError")),
        _error_message(item.get("lastError")),
    )


def _can_view_monitoring_record(
    request: Request | None,
    *records: dict[str, Any] | None,
) -> bool:
    user = get_request_user(request)
    if user is None or is_admin_user(user):
        return True

    owner_ids = {owner_id for record in records if (owner_id := record_owner_id(record))}
    if not owner_ids:
        return True
    return str(user.get("userId") or "") in owner_ids


def _error_record_from_event(event: dict, request: Request | None = None) -> Optional[dict]:
    error = event.get("error")
    if not error:
        return None
    item = JOB_ITEMS.get(str(event.get("jobItemId"))) if event.get("jobItemId") else None
    doc = DOCUMENTS.get(str(item.get("documentId"))) if item and item.get("documentId") else None
    job = JOBS.get(str(event.get("jobId"))) if event.get("jobId") else None
    if not _can_view_monitoring_record(request, item, doc, job):
        return None
    return {
        "errorId": _event_error_id(event),
        "source": "jobEvent",
        "severity": "error" if event.get("eventType") == "FAILED" else "warning",
        "type": _error_code(error),
        "message": _error_message(error),
        "fileName": (item or doc or {}).get("fileName"),
        "filePath": (item or {}).get("sourcePath") or (doc or {}).get("sourcePath"),
        "page": None,
        "jobId": event.get("jobId"),
        "jobItemId": event.get("jobItemId"),
        "documentId": (item or {}).get("documentId"),
        "model": (job or {}).get("modelId") or (doc or {}).get("modelCode"),
        "status": event.get("status"),
        "retryCount": event.get("retryCount"),
        "occurredAt": event.get("createdAt"),
        "raw": error,
    }


def _error_record_from_item(item: dict, request: Request | None = None) -> Optional[dict]:
    error = item.get("lastError")
    if not error or item.get("status") not in {"FAILED", "QUEUED"}:
        return None
    doc = DOCUMENTS.get(str(item.get("documentId"))) if item.get("documentId") else None
    job = JOBS.get(str(item.get("jobId"))) if item.get("jobId") else None
    if not _can_view_monitoring_record(request, item, doc, job):
        return None
    return {
        "errorId": _item_error_id(item),
        "source": "jobItem",
        "severity": "error" if item.get("status") == "FAILED" else "warning",
        "type": _error_code(error),
        "message": _error_message(error),
        "fileName": item.get("fileName") or (doc or {}).get("fileName"),
        "filePath": item.get("sourcePath") or (doc or {}).get("sourcePath"),
        "page": None,
        "jobId": item.get("jobId"),
        "jobItemId": item.get("jobItemId"),
        "documentId": item.get("documentId"),
        "model": (job or {}).get("modelId") or (doc or {}).get("modelCode"),
        "status": item.get("status"),
        "retryCount": item.get("retryCount"),
        "occurredAt": item.get("updatedAt") or item.get("completedAt"),
        "raw": error,
    }


def _collect_error_records(request: Request | None = None) -> list[dict]:
    records: dict[str, dict] = {}
    event_item_ids: set[str] = set()
    for event in JOB_EVENTS.values():
        record = _error_record_from_event(event, request)
        if record:
            records[record["errorId"]] = record
            if record.get("jobItemId"):
                event_item_ids.add(str(record["jobItemId"]))
    for item in JOB_ITEMS.values():
        if str(item.get("jobItemId")) in event_item_ids:
            continue
        record = _error_record_from_item(item, request)
        if record:
            records.setdefault(record["errorId"], record)
    items = list(records.values())
    items.sort(key=lambda item: item.get("occurredAt") or "", reverse=True)
    return items


@router.get(
    "/system",
    summary="실시간 시스템 모니터링",
    description="CPU load average, 작업 상태 카운트, 입력/출력 스토리지 사용량을 반환합니다.",
)
def system_monitoring(request: Request):
    config = get_config()
    load_average = os.getloadavg() if hasattr(os, "getloadavg") else None
    storage_path = get_configured_storage_path(config.output_root)
    visible_jobs = filter_records_for_user(request, JOBS.values())
    return ok(
        {
            "cpu": {
                "loadAverage1m": load_average[0] if load_average else None,
                "loadAverage5m": load_average[1] if load_average else None,
                "loadAverage15m": load_average[2] if load_average else None,
                "cpuCount": os.cpu_count(),
            },
            "jobs": {
                "queued": sum(1 for job in visible_jobs if job.get("status") == "QUEUED"),
                "processing": sum(1 for job in visible_jobs if job.get("status") == "PROCESSING"),
                "completed": sum(1 for job in visible_jobs if job.get("status") == "COMPLETED"),
                "failed": sum(1 for job in visible_jobs if job.get("status") == "FAILED"),
            },
            "storage": {
                "input": disk_usage(config.input_root),
                "output": disk_usage(storage_path),
                "tmp": disk_usage(config.tmp_root),
            },
        }
    )


@router.get(
    "/errors/summary",
    summary="에러 로그 요약",
    description="대시보드 상단 에러 유형별 통계와 최근 에러 항목을 반환합니다.",
)
def error_summary(request: Request):
    records = _collect_error_records(request)
    by_type: dict[str, int] = {}
    for record in records:
        by_type[record["type"]] = by_type.get(record["type"], 0) + 1
    return ok({"totalErrors": len(records), "byType": by_type, "recent": records[:5]})


@router.get(
    "/errors",
    summary="에러 로그 목록 조회",
    description="에러 로그 테이블을 위한 검색, 유형/심각도 필터, 페이지네이션 결과를 반환합니다.",
)
def list_errors(
    request: Request,
    q: Optional[str] = Query(default=None, max_length=120),
    severity: Optional[Literal["warning", "error"]] = None,
    type_: Optional[str] = Query(default=None, alias="type", max_length=80),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    records = _collect_error_records(request)
    if q:
        needle = q.lower()
        records = [
            item
            for item in records
            if needle in str(item.get("message", "")).lower()
            or needle in str(item.get("fileName", "")).lower()
            or needle in str(item.get("filePath", "")).lower()
        ]
    if severity:
        records = [item for item in records if item.get("severity") == severity]
    if type_:
        records = [item for item in records if item.get("type") == type_]

    return ok({"items": records[offset : offset + limit], "total": len(records), "limit": limit, "offset": offset})


@router.get(
    "/errors/{errorId}",
    summary="에러 상세 정보 조회",
    description="에러 메시지, 파일 정보, 작업 정보, 원본 에러 payload와 권장 조치 문구를 반환합니다.",
)
def get_error_detail(errorId: str, request: Request):
    for record in _collect_error_records(request):
        if record["errorId"] == errorId:
            detail = dict(record)
            detail["recommendedActions"] = [
                "원본 파일 손상 여부 확인",
                "파일 형식과 확장자 확인",
                "동일 작업 재시도 또는 모델 변경 후 재처리",
            ]
            return ok(detail)
    fail("NOT_FOUND", f"error not found: {errorId}", status=404)

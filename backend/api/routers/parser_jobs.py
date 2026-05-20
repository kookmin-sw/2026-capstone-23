from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Query, Request

from api.common import ErrorCode, fail, fail_not_found, now_iso, ok
from api.security import require_record_access
from core.jobs.service import (
    build_item_timings,
    force_cancel,
    list_item_events,
    list_job_events,
    list_job_items,
    request_cancel,
)
from infra.queue.job_queue import queue_backend_name, queue_sizes, queue_status
from infra.store import JOBS


router = APIRouter(tags=["parser"])


def _require_job_access(request: Request | None, job_id: str) -> Dict[str, Any]:
    job = JOBS.get(job_id)
    if not job:
        fail_not_found("job", job_id)
    require_record_access(request, job, resource="job", resource_id=job_id)
    return job


@router.get(
    "/jobs/{jobId}/items",
    summary="Job item list",
    description="Returns the items and recent events for a parser job.",
)
def get_job_items(request: Request, jobId: str):
    job = _require_job_access(request, jobId)
    items = list_job_items(jobId)
    if not items and job.get("items"):
        return ok({"jobId": jobId, "items": job.get("items", [])})
    for item in items:
        item_id = item.get("jobItemId")
        item["events"] = (
            list_item_events(jobId, str(item_id), limit=20) if item_id else []
        )
        item.update(build_item_timings(item))
    return ok({"jobId": jobId, "items": items})


@router.get(
    "/jobs/{jobId}/events",
    summary="Job event list",
    description="Returns recent state transition events for a parser job or one job item.",
)
def get_job_events(
    request: Request,
    jobId: str,
    limit: int = Query(default=100, ge=1, le=500),
    jobItemId: Optional[str] = Query(default=None),
):
    _require_job_access(request, jobId)
    events = list_job_events(job_id=jobId, limit=limit, job_item_id=jobItemId)
    return ok({"jobId": jobId, "events": events})


@router.get(
    "/queue/stats",
    summary="Queue stats",
    description="Returns the active queue backend, readiness state, and per-route queue sizes.",
)
def get_queue_stats():
    queues = queue_sizes()
    return ok(
        {
            "backend": queue_backend_name(),
            "status": queue_status(),
            "queuedItems": sum(queues.values()),
            "queues": queues,
        }
    )


@router.get(
    "/jobs/{jobId}",
    summary="Job status",
    description="Returns aggregate status for a parser job.",
)
def get_job_status(request: Request, jobId: str):
    job = _require_job_access(request, jobId)
    return ok(job)


@router.post(
    "/jobs/{jobId}/cancel",
    summary="Cancel job",
    description="Requests cooperative cancellation for a running parser job.",
)
def cancel_job(request: Request, jobId: str, force: bool = Query(default=False)):
    job = _require_job_access(request, jobId)

    current_status = job.get("status")
    if current_status in {"COMPLETED", "FAILED", "CANCELED"}:
        fail(
            ErrorCode.CONFLICT,
            f"job is already finalized: {current_status}",
            status=409,
            details={"jobId": jobId, "status": current_status},
        )

    canceled_items = force_cancel(jobId) if force else request_cancel(jobId)
    updated_job = JOBS.get(jobId)
    if not updated_job:
        fail_not_found("job", jobId)
    updated_job["canceledAt"] = now_iso()
    return ok(
        {
            "ok": True,
            "jobId": jobId,
            "status": "CANCELING",
            "force": force,
            "cancelRequestedAt": updated_job["canceledAt"],
            "cancelAppliedItems": canceled_items,
            "message": "force cancel applied"
            if force
            else "cancel requested"
            if canceled_items > 0
            else "cancel requested (no queued items to cancel)",
        }
    )

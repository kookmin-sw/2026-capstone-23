from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Optional, Set

from core.env import env_int
from core.jobs.execution import QWEN_FINALIZE_QUEUE_ROUTE, QWEN_INFER_QUEUE_ROUTE
from core.jobs.service import mark_item_progress
from core.time import now_iso
from infra.queue.job_queue import enqueue as enqueue_queue
from infra.store import QWEN_FINALIZE_TASKS, QWEN_INFER_RESULTS, QWEN_INFER_TASKS, QWEN_PREPROCESS_TASKS

FINAL_TASK_STATES = {"COMPLETED", "FAILED", "CANCELED"}
_QWEN_INFER_PROGRESS_START = 20
_QWEN_INFER_PROGRESS_END = 85


def _stable_id(prefix: str, *parts: object) -> str:
    digest = hashlib.sha1(
        "|".join(str(part) for part in parts).encode("utf-8"),
        usedforsecurity=False,
    ).hexdigest()[:12]
    return f"{prefix}_{digest}"


def infer_task_id_for(job_item_id: str, image_id: str) -> str:
    return _stable_id("qi", job_item_id, image_id)


def finalize_task_id_for(job_item_id: str) -> str:
    return _stable_id("qf", job_item_id, "finalize")


def _enqueue_infer_task(task: Dict[str, Any], *, attempt: Optional[int] = None) -> None:
    enqueue_queue(
        str(task["taskId"]),
        job_id=str(task.get("jobId") or ""),
        attempt=int(task.get("retryCount", 0) if attempt is None else attempt),
        queued_at=str(task.get("queuedAt") or now_iso()),
        queue_route=QWEN_INFER_QUEUE_ROUTE,
    )


def _enqueue_finalize_task(task: Dict[str, Any], *, attempt: Optional[int] = None) -> None:
    enqueue_queue(
        str(task["taskId"]),
        job_id=str(task.get("jobId") or ""),
        attempt=int(task.get("retryCount", 0) if attempt is None else attempt),
        queued_at=str(task.get("queuedAt") or now_iso()),
        queue_route=QWEN_FINALIZE_QUEUE_ROUTE,
    )


def save_preprocess_payload(job_item_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    stored = dict(payload)
    stored["jobItemId"] = job_item_id
    stored["updatedAt"] = now_iso()
    QWEN_PREPROCESS_TASKS[job_item_id] = stored
    return QWEN_PREPROCESS_TASKS[job_item_id]


def get_preprocess_payload(job_item_id: str) -> Optional[Dict[str, Any]]:
    return QWEN_PREPROCESS_TASKS.get(job_item_id)


def list_infer_tasks(job_item_id: str) -> List[Dict[str, Any]]:
    tasks = [task for task in QWEN_INFER_TASKS.values() if task.get("jobItemId") == job_item_id]
    tasks.sort(key=lambda task: (int(task.get("pageNum", 0)), str(task.get("imageId") or "")))
    return tasks


def list_infer_results(job_item_id: str) -> List[Dict[str, Any]]:
    results = [result for result in QWEN_INFER_RESULTS.values() if result.get("jobItemId") == job_item_id]
    results.sort(key=lambda result: (int(result.get("pageNum", 0)), str(result.get("imageId") or "")))
    return results


def count_infer_tasks(job_item_id: str) -> int:
    return len(list_infer_tasks(job_item_id))


def all_infer_tasks_completed(job_item_id: str) -> bool:
    tasks = list_infer_tasks(job_item_id)
    if not tasks:
        return True
    return all(str(task.get("status") or "") in FINAL_TASK_STATES for task in tasks)


def all_infer_tasks_succeeded(job_item_id: str) -> bool:
    tasks = list_infer_tasks(job_item_id)
    if not tasks:
        return True
    return all(str(task.get("status") or "") == "COMPLETED" for task in tasks)


def _publish_infer_progress(job_item_id: str, *, worker_id: str | None = None) -> None:
    tasks = list_infer_tasks(job_item_id)
    total = len(tasks)
    if total <= 0:
        return

    completed = sum(1 for task in tasks if str(task.get("status") or "") == "COMPLETED")
    scaled_percent = _QWEN_INFER_PROGRESS_START + int(
        (completed / total) * (_QWEN_INFER_PROGRESS_END - _QWEN_INFER_PROGRESS_START)
    )
    mark_item_progress(
        job_item_id,
        completed,
        total,
        worker_id=worker_id,
        stage="GPU_PROCESSING",
        progress_percent=scaled_percent,
        detail={"completedInferenceTasks": completed, "totalInferenceTasks": total},
    )


def has_infer_failures(job_item_id: str) -> bool:
    return any(str(task.get("status") or "") == "FAILED" for task in list_infer_tasks(job_item_id))


def prune_infer_tasks(job_item_id: str, keep_task_ids: Set[str]) -> int:
    removed = 0
    for task in list(list_infer_tasks(job_item_id)):
        task_id = str(task.get("taskId") or "")
        if not task_id or task_id in keep_task_ids:
            continue
        QWEN_INFER_TASKS.pop(task_id, None)
        QWEN_INFER_RESULTS.pop(task_id, None)
        removed += 1
    return removed


def requeue_infer_task(
    task_id: str,
    *,
    error: Any = None,
    increment_retry: bool = False,
) -> bool:
    task = QWEN_INFER_TASKS.get(task_id)
    if not task:
        return False

    now = now_iso()
    if increment_retry:
        task["retryCount"] = int(task.get("retryCount", 0)) + 1

    task["status"] = "QUEUED"
    task["lastError"] = error
    task["workerId"] = None
    task["queuedAt"] = now
    task["startedAt"] = None
    task["completedAt"] = None
    task["updatedAt"] = now
    QWEN_INFER_RESULTS.pop(task_id, None)
    _enqueue_infer_task(task)
    return True


def requeue_incomplete_infer_tasks(
    job_item_id: str,
    *,
    error: Any = None,
    include_failed: bool = False,
) -> int:
    count = 0
    for task in list_infer_tasks(job_item_id):
        status = str(task.get("status") or "")
        if status == "COMPLETED":
            continue
        if status == "FAILED" and not include_failed:
            continue
        if requeue_infer_task(str(task["taskId"]), error=error, increment_retry=False):
            count += 1
    return count


def create_infer_task(
    *,
    job_id: str,
    job_item_id: str,
    document_id: str,
    page_num: int,
    image_id: str,
    image_path: str,
    language: str,
    model_code: str,
    bbox: list[float],
    is_table: Optional[bool] = None,
    is_flowchart: Optional[bool] = None,
    is_math: Optional[bool] = None,
    max_retries: int = 2,
    requeue_existing: bool = False,
) -> Dict[str, Any]:
    task_id = infer_task_id_for(job_item_id, image_id)
    existing = QWEN_INFER_TASKS.get(task_id)
    if existing is not None:
        existing["jobId"] = job_id
        existing["jobItemId"] = job_item_id
        existing["documentId"] = document_id
        existing["pageNum"] = int(page_num)
        existing["imageId"] = image_id
        existing["imagePath"] = image_path
        existing["language"] = language
        existing["modelCode"] = model_code
        existing["bbox"] = list(bbox or [0, 0, 0, 0])
        existing["isTable"] = is_table
        existing["isFlowchart"] = is_flowchart
        existing["isMath"] = is_math
        existing["maxRetries"] = max(0, int(max_retries))
        existing["updatedAt"] = now_iso()
        if requeue_existing and str(existing.get("status") or "") != "COMPLETED":
            requeue_infer_task(task_id, error=None, increment_retry=False)
        return existing

    queued_at = now_iso()
    QWEN_INFER_TASKS[task_id] = {
        "taskId": task_id,
        "jobId": job_id,
        "jobItemId": job_item_id,
        "documentId": document_id,
        "pageNum": int(page_num),
        "imageId": image_id,
        "imagePath": image_path,
        "language": language,
        "modelCode": model_code,
        "bbox": list(bbox or [0, 0, 0, 0]),
        "isTable": is_table,
        "isFlowchart": is_flowchart,
        "isMath": is_math,
        "status": "QUEUED",
        "retryCount": 0,
        "maxRetries": max(0, int(max_retries)),
        "lastError": None,
        "workerId": None,
        "queuedAt": queued_at,
        "startedAt": None,
        "completedAt": None,
        "updatedAt": queued_at,
    }
    _enqueue_infer_task(QWEN_INFER_TASKS[task_id], attempt=0)
    return QWEN_INFER_TASKS[task_id]


def mark_infer_task_processing(task_id: str, worker_id: str) -> bool:
    task = QWEN_INFER_TASKS.get(task_id)
    if not task or str(task.get("status") or "") != "QUEUED":
        return False

    now = now_iso()
    task["status"] = "PROCESSING"
    task["workerId"] = worker_id
    task["startedAt"] = now
    task["updatedAt"] = now
    return True


def mark_infer_task_completed(task_id: str, worker_id: str, result: Dict[str, Any]) -> bool:
    task = QWEN_INFER_TASKS.get(task_id)
    if not task or str(task.get("status") or "") in FINAL_TASK_STATES:
        return False

    now = now_iso()
    task["status"] = "COMPLETED"
    task["workerId"] = worker_id
    task["completedAt"] = now
    task["updatedAt"] = now
    task["lastError"] = None

    QWEN_INFER_RESULTS[task_id] = {
        "taskId": task_id,
        "jobId": task.get("jobId"),
        "jobItemId": task.get("jobItemId"),
        "documentId": task.get("documentId"),
        "pageNum": task.get("pageNum"),
        "imageId": task.get("imageId"),
        "bbox": list(task.get("bbox") or [0, 0, 0, 0]),
        "workerId": worker_id,
        "result": result,
        "completedAt": now,
    }
    _publish_infer_progress(str(task.get("jobItemId") or ""), worker_id=worker_id)
    return True


def mark_infer_task_retry(task_id: str, error: Any) -> bool:
    task = QWEN_INFER_TASKS.get(task_id)
    if not task or str(task.get("status") or "") != "PROCESSING":
        return False
    return requeue_infer_task(task_id, error=error, increment_retry=True)


def mark_infer_task_failed(task_id: str, worker_id: str, error: Any) -> bool:
    task = QWEN_INFER_TASKS.get(task_id)
    if not task or str(task.get("status") or "") in FINAL_TASK_STATES:
        return False

    now = now_iso()
    task["status"] = "FAILED"
    task["workerId"] = worker_id
    task["lastError"] = error
    task["completedAt"] = now
    task["updatedAt"] = now
    QWEN_INFER_RESULTS.pop(task_id, None)
    return True


def ensure_finalize_task(
    *,
    job_id: str,
    job_item_id: str,
    document_id: str,
    enqueue_now: bool = True,
) -> Dict[str, Any]:
    task_id = finalize_task_id_for(job_item_id)
    existing = QWEN_FINALIZE_TASKS.get(task_id)
    if existing is not None:
        if enqueue_now and str(existing.get("status") or "") == "QUEUED":
            _enqueue_finalize_task(existing)
        return existing

    queued_at = now_iso()
    max_retries = env_int("QWEN_FINALIZE_MAX_RETRIES", 2, minimum=0)
    QWEN_FINALIZE_TASKS[task_id] = {
        "taskId": task_id,
        "jobId": job_id,
        "jobItemId": job_item_id,
        "documentId": document_id,
        "status": "QUEUED",
        "retryCount": 0,
        "maxRetries": max_retries,
        "workerId": None,
        "lastError": None,
        "queuedAt": queued_at,
        "startedAt": None,
        "completedAt": None,
        "updatedAt": queued_at,
        "outputPath": "",
    }
    if enqueue_now:
        _enqueue_finalize_task(QWEN_FINALIZE_TASKS[task_id], attempt=0)
    return QWEN_FINALIZE_TASKS[task_id]


def get_finalize_task(task_id: str) -> Optional[Dict[str, Any]]:
    return QWEN_FINALIZE_TASKS.get(task_id)


def get_finalize_task_for_item(job_item_id: str) -> Optional[Dict[str, Any]]:
    return QWEN_FINALIZE_TASKS.get(finalize_task_id_for(job_item_id))


def requeue_finalize_task(
    task_id: str,
    *,
    error: Any = None,
    increment_retry: bool = False,
    enqueue_now: bool = True,
) -> bool:
    task = QWEN_FINALIZE_TASKS.get(task_id)
    if not task:
        return False

    now = now_iso()
    if increment_retry:
        task["retryCount"] = int(task.get("retryCount", 0)) + 1

    task["status"] = "QUEUED"
    task["workerId"] = None
    task["lastError"] = error
    task["queuedAt"] = now
    task["startedAt"] = None
    task["completedAt"] = None
    task["updatedAt"] = now
    task["outputPath"] = ""
    if enqueue_now:
        _enqueue_finalize_task(task)
    return True


def reset_finalize_task_for_item(
    job_item_id: str,
    *,
    error: Any = None,
    enqueue_now: bool = False,
) -> Optional[Dict[str, Any]]:
    task = get_finalize_task_for_item(job_item_id)
    if task is None:
        return None
    requeue_finalize_task(str(task["taskId"]), error=error, increment_retry=False, enqueue_now=enqueue_now)
    return task


def mark_finalize_task_processing(task_id: str, worker_id: str) -> bool:
    task = QWEN_FINALIZE_TASKS.get(task_id)
    if not task or str(task.get("status") or "") != "QUEUED":
        return False

    now = now_iso()
    task["status"] = "PROCESSING"
    task["workerId"] = worker_id
    task["startedAt"] = now
    task["updatedAt"] = now
    return True


def mark_finalize_task_completed(task_id: str, worker_id: str, output_path: str) -> bool:
    task = QWEN_FINALIZE_TASKS.get(task_id)
    if not task or str(task.get("status") or "") in FINAL_TASK_STATES:
        return False

    now = now_iso()
    task["status"] = "COMPLETED"
    task["workerId"] = worker_id
    task["outputPath"] = output_path
    task["lastError"] = None
    task["completedAt"] = now
    task["updatedAt"] = now
    return True


def mark_finalize_task_retry(task_id: str, error: Any) -> bool:
    task = QWEN_FINALIZE_TASKS.get(task_id)
    if not task or str(task.get("status") or "") != "PROCESSING":
        return False
    return requeue_finalize_task(task_id, error=error, increment_retry=True, enqueue_now=True)


def mark_finalize_task_failed(task_id: str, worker_id: str, error: Any) -> bool:
    task = QWEN_FINALIZE_TASKS.get(task_id)
    if not task or str(task.get("status") or "") in FINAL_TASK_STATES:
        return False

    now = now_iso()
    task["status"] = "FAILED"
    task["workerId"] = worker_id
    task["lastError"] = error
    task["completedAt"] = now
    task["updatedAt"] = now
    return True

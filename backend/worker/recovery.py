import time
from threading import Event
from typing import Any, Optional

from core.jobs.service import (
    cleanup_old_job_events,
    mark_item_final,
    mark_item_retry,
    mark_item_stage,
    parse_iso8601,
    utc_now,
)
from core.jobs.qwen_stage_service import (
    all_infer_tasks_completed,
    all_infer_tasks_succeeded,
    count_infer_tasks,
    ensure_finalize_task,
    get_finalize_task_for_item,
    has_infer_failures,
    mark_finalize_task_failed,
    mark_finalize_task_retry,
    mark_infer_task_failed,
    mark_infer_task_retry,
)
from core.env import env_float, env_int
from core.jobs.worker_lease_service import get_lease, lease_is_active
from infra.store import JOB_ITEMS, QWEN_FINALIZE_TASKS, QWEN_INFER_TASKS


def _retry_or_fail_item(
    item_id: str, item: dict[str, Any], error: dict[str, Any]
) -> None:
    retry_count = int(item.get("retryCount", 0))
    max_retries = int(item.get("maxRetries", 3))
    if retry_count < max_retries:
        mark_item_retry(item_id, error=error)
        return
    mark_item_final(item_id, "FAILED", error=error)


def _recover_legacy_processing_items(now) -> None:
    for item_id, item in list(JOB_ITEMS.items()):
        if item.get("status") != "PROCESSING":
            continue
        if str(item.get("executionBackend") or "") == "qwen_gpu":
            continue

        started_at = parse_iso8601(item.get("startedAt"))
        if not started_at:
            continue

        timeout_seconds = int(
            item.get(
                "timeoutSeconds", env_int("JOB_ITEM_TIMEOUT_SECONDS", 300, minimum=1)
            )
        )
        elapsed = (now - started_at).total_seconds()
        if elapsed <= timeout_seconds:
            continue

        _retry_or_fail_item(
            item_id,
            item,
            {
                "code": "RECOVERY_TIMEOUT",
                "message": "processing stuck; requeued by recovery worker",
            },
        )


def _recover_qwen_preprocessing_items() -> None:
    for item_id, item in list(JOB_ITEMS.items()):
        if item.get("status") != "PROCESSING":
            continue
        if str(item.get("executionBackend") or "") != "qwen_gpu":
            continue
        if str(item.get("stage") or "") != "PREPROCESSING":
            continue

        lease = get_lease("qwen_doc_item", item_id)
        if lease_is_active(lease):
            continue

        _retry_or_fail_item(
            item_id,
            item,
            {
                "code": "RECOVERY_QWEN_DOC_LEASE_EXPIRED",
                "message": "qwen preprocess worker lease expired",
            },
        )


def _recover_qwen_waiting_items() -> None:
    for item_id, item in list(JOB_ITEMS.items()):
        if item.get("status") != "PROCESSING":
            continue
        if str(item.get("executionBackend") or "") != "qwen_gpu":
            continue

        stage = str(item.get("stage") or "")
        if stage not in {"GPU_WAITING", "GPU_PROCESSING"}:
            continue

        infer_task_count = count_infer_tasks(item_id)
        if infer_task_count <= 0:
            _retry_or_fail_item(
                item_id,
                item,
                {
                    "code": "RECOVERY_QWEN_INFER_TASKS_MISSING",
                    "message": "item waiting for qwen inference without staged infer tasks",
                },
            )
            continue

        if not all_infer_tasks_completed(item_id):
            continue

        if not all_infer_tasks_succeeded(item_id):
            mark_item_final(
                item_id,
                "FAILED",
                error={
                    "code": "RECOVERY_QWEN_INFER_FAILED",
                    "message": "one or more qwen inference tasks failed",
                },
            )
            continue

        if get_finalize_task_for_item(item_id) is None:
            ensure_finalize_task(
                job_id=str(item["jobId"]),
                job_item_id=item_id,
                document_id=str(item["documentId"]),
                enqueue_now=True,
            )


def _recover_qwen_postprocessing_items() -> None:
    for item_id, item in list(JOB_ITEMS.items()):
        if item.get("status") != "PROCESSING":
            continue
        if str(item.get("executionBackend") or "") != "qwen_gpu":
            continue
        if str(item.get("stage") or "") != "POSTPROCESSING":
            continue

        finalize_task = get_finalize_task_for_item(item_id)
        if finalize_task is None:
            if all_infer_tasks_completed(item_id) and all_infer_tasks_succeeded(
                item_id
            ):
                ensure_finalize_task(
                    job_id=str(item["jobId"]),
                    job_item_id=item_id,
                    document_id=str(item["documentId"]),
                    enqueue_now=True,
                )
            elif has_infer_failures(item_id):
                mark_item_final(
                    item_id,
                    "FAILED",
                    error={
                        "code": "RECOVERY_QWEN_INFER_FAILED",
                        "message": "qwen finalize blocked because infer tasks failed",
                    },
                )
            continue

        if str(finalize_task.get("status") or "") == "FAILED":
            mark_item_final(
                item_id,
                "FAILED",
                error=dict(
                    finalize_task.get("lastError")
                    or {
                        "code": "RECOVERY_QWEN_FINALIZE_FAILED",
                        "message": "qwen finalize task failed",
                    }
                ),
            )


def _recover_stale_infer_tasks(worker_id: str) -> None:
    for task_id, task in list(QWEN_INFER_TASKS.items()):
        if str(task.get("status") or "") != "PROCESSING":
            continue

        lease = get_lease("qwen_infer_task", task_id)
        if lease_is_active(lease):
            continue

        error = {
            "code": "RECOVERY_QWEN_INFER_LEASE_EXPIRED",
            "message": "qwen infer worker lease expired",
        }
        retry_count = int(task.get("retryCount", 0))
        max_retries = int(task.get("maxRetries", 0))
        job_item_id = str(task.get("jobItemId") or "")

        if retry_count < max_retries:
            if mark_infer_task_retry(task_id, error):
                mark_item_stage(
                    job_item_id, "GPU_WAITING", worker_id=worker_id, error=error
                )
            continue

        if mark_infer_task_failed(task_id, worker_id, error):
            mark_item_final(job_item_id, "FAILED", error=error)


def _recover_stale_finalize_tasks(worker_id: str) -> None:
    for task_id, task in list(QWEN_FINALIZE_TASKS.items()):
        if str(task.get("status") or "") != "PROCESSING":
            continue

        lease = get_lease("qwen_finalize_task", task_id)
        if lease_is_active(lease):
            continue

        error = {
            "code": "RECOVERY_QWEN_FINALIZE_LEASE_EXPIRED",
            "message": "qwen finalize worker lease expired",
        }
        retry_count = int(task.get("retryCount", 0))
        max_retries = int(
            task.get("maxRetries", env_int("QWEN_FINALIZE_MAX_RETRIES", 2, minimum=1))
        )
        job_item_id = str(task.get("jobItemId") or "")

        if retry_count < max_retries:
            if mark_finalize_task_retry(task_id, error):
                mark_item_stage(
                    job_item_id, "POSTPROCESSING", worker_id=worker_id, error=error
                )
            continue

        if mark_finalize_task_failed(task_id, worker_id, error):
            mark_item_final(job_item_id, "FAILED", error=error)


def run_recovery_worker(
    worker_id: str = "recovery-worker-1",
    scan_interval: float = 5.0,
    stop_event: Optional[Event] = None,
) -> None:
    print(f"[recovery] started: {worker_id}")
    cleanup_interval = env_float("JOB_EVENTS_CLEANUP_INTERVAL_SECONDS", 60)
    next_cleanup_at = time.monotonic() + cleanup_interval
    while True:
        if stop_event and stop_event.is_set():
            print(f"[recovery] stopped: {worker_id}")
            return

        now = utc_now()
        _recover_legacy_processing_items(now)
        _recover_qwen_preprocessing_items()
        _recover_stale_infer_tasks(worker_id)
        _recover_qwen_waiting_items()
        _recover_stale_finalize_tasks(worker_id)
        _recover_qwen_postprocessing_items()

        if cleanup_interval > 0 and time.monotonic() >= next_cleanup_at:
            removed = cleanup_old_job_events()
            if removed > 0:
                print(f"[recovery] cleaned old job events: {removed}")
            next_cleanup_at = time.monotonic() + cleanup_interval

        time.sleep(scan_interval)


if __name__ == "__main__":
    run_recovery_worker()

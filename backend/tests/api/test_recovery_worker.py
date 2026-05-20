from datetime import timedelta
import os

os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("QUEUE_BACKEND", "memory")
os.environ.setdefault("STORE_BACKEND", "memory")
os.environ.setdefault("STATUS_CACHE_BACKEND", "none")
os.environ.setdefault("QWEN_WORKER_LEASE_TTL_SECONDS", "3600")

from core.jobs.qwen_stage_service import (
    create_infer_task,
    ensure_finalize_task,
    get_finalize_task_for_item,
    mark_finalize_task_completed,
    mark_infer_task_completed,
    mark_infer_task_processing,
)
from core.jobs.service import create_job, create_job_item, mark_item_processing
from core.jobs.worker_lease_service import acquire_lease
from core.time import now_iso
from infra.queue.job_queue import QUEUE
from infra.store import (
    DOCUMENTS,
    JOB_EVENTS,
    JOB_ITEMS,
    JOBS,
    QWEN_FINALIZE_TASKS,
    QWEN_INFER_RESULTS,
    QWEN_INFER_TASKS,
    QWEN_PREPROCESS_TASKS,
    WORKER_LEASES,
)
from worker.recovery import (
    _recover_qwen_postprocessing_items,
    _recover_qwen_preprocessing_items,
    _recover_stale_infer_tasks,
)
from storage.sqlite_files import clear_file_blobs


def setup_function() -> None:
    JOBS.clear()
    JOB_ITEMS.clear()
    JOB_EVENTS.clear()
    DOCUMENTS.clear()
    QWEN_PREPROCESS_TASKS.clear()
    QWEN_INFER_TASKS.clear()
    QWEN_INFER_RESULTS.clear()
    QWEN_FINALIZE_TASKS.clear()
    WORKER_LEASES.clear()
    clear_file_blobs()
    for route in (
        None,
        "openai",
        "openrouter",
        "qwen_gpu",
        "qwen_doc",
        "qwen_infer",
        "qwen_finalize",
    ):
        while True:
            item_id = QUEUE.dequeue(queue_route=route)
            if item_id is None:
                break


def _stale_iso(seconds: int) -> str:
    from datetime import datetime, timezone

    return (
        datetime.now(timezone.utc) - timedelta(seconds=seconds)
    ).isoformat().replace("+00:00", "Z")


def _create_qwen_item(stage: str) -> tuple[str, str]:
    job_id = create_job(
        model_id="m3",
        parallelism=1,
        total_items=1,
        requested_execution_backend="qwen_gpu",
        execution_backend="qwen_gpu",
    )
    item_id = create_job_item(
        job_id=job_id,
        document_id=f"d_{stage.lower()}",
        file_name="sample.pdf",
        source_path="sample.pdf",
        language="ko",
        execution_backend="qwen_gpu",
        queue_route="qwen_doc",
        model_code="qwen2.5-vl-7b",
        timeout_seconds=3600,
        max_retries=3,
    )
    assert mark_item_processing(item_id, "worker-1", stage=stage) is True
    JOB_ITEMS[item_id]["startedAt"] = now_iso()
    return job_id, item_id


def test_recovery_leaves_qwen_infer_task_with_active_lease_untouched() -> None:
    job_id, item_id = _create_qwen_item("GPU_PROCESSING")
    task = create_infer_task(
        job_id=job_id,
        job_item_id=item_id,
        document_id="d_active",
        page_num=1,
        image_id="page-1-img-1",
        image_path="image.png",
        language="ko",
        model_code="qwen2.5-vl-7b",
        bbox=[0, 0, 10, 10],
        max_retries=2,
    )
    assert mark_infer_task_processing(str(task["taskId"]), "infer-worker") is True
    acquire_lease(
        kind="qwen_infer_task",
        target_id=str(task["taskId"]),
        worker_id="infer-worker",
        job_id=job_id,
        job_item_id=item_id,
    )

    _recover_stale_infer_tasks("recovery-test")

    assert QWEN_INFER_TASKS[str(task["taskId"])]["status"] == "PROCESSING"
    assert QWEN_INFER_TASKS[str(task["taskId"])]["retryCount"] == 0


def test_recovery_requeues_qwen_infer_task_when_lease_expired() -> None:
    job_id, item_id = _create_qwen_item("GPU_PROCESSING")
    task = create_infer_task(
        job_id=job_id,
        job_item_id=item_id,
        document_id="d_expired",
        page_num=1,
        image_id="page-1-img-1",
        image_path="image.png",
        language="ko",
        model_code="qwen2.5-vl-7b",
        bbox=[0, 0, 10, 10],
        max_retries=2,
    )
    assert mark_infer_task_processing(str(task["taskId"]), "infer-worker") is True
    lease = acquire_lease(
        kind="qwen_infer_task",
        target_id=str(task["taskId"]),
        worker_id="infer-worker",
        job_id=job_id,
        job_item_id=item_id,
    )
    WORKER_LEASES[str(lease["leaseId"])]["leaseExpiresAt"] = _stale_iso(10)

    _recover_stale_infer_tasks("recovery-test")

    recovered_task = QWEN_INFER_TASKS[str(task["taskId"])]
    assert recovered_task["status"] == "QUEUED"
    assert recovered_task["retryCount"] == 1
    assert JOB_ITEMS[item_id]["stage"] == "GPU_WAITING"
    assert JOB_ITEMS[item_id]["status"] == "PROCESSING"


def test_recovery_retries_qwen_preprocess_item_when_doc_lease_missing() -> None:
    _job_id, item_id = _create_qwen_item("PREPROCESSING")

    _recover_qwen_preprocessing_items()

    assert JOB_ITEMS[item_id]["status"] == "QUEUED"
    assert JOB_ITEMS[item_id]["retryCount"] == 1


def test_recovery_recreates_finalize_task_from_state_invariant() -> None:
    job_id, item_id = _create_qwen_item("POSTPROCESSING")
    task = create_infer_task(
        job_id=job_id,
        job_item_id=item_id,
        document_id="d_finalize",
        page_num=1,
        image_id="page-1-img-1",
        image_path="image.png",
        language="ko",
        model_code="qwen2.5-vl-7b",
        bbox=[0, 0, 10, 10],
        max_retries=2,
    )
    mark_infer_task_completed(str(task["taskId"]), "infer-worker", {"text": "ok"})

    _recover_qwen_postprocessing_items()

    finalize_task = get_finalize_task_for_item(item_id)
    assert finalize_task is not None
    assert finalize_task["status"] == "QUEUED"


def test_canceled_qwen_infer_task_ignores_late_worker_completion() -> None:
    job_id, item_id = _create_qwen_item("GPU_PROCESSING")
    task = create_infer_task(
        job_id=job_id,
        job_item_id=item_id,
        document_id="d_force_canceled_infer",
        page_num=1,
        image_id="page-1-img-1",
        image_path="image.png",
        language="ko",
        model_code="qwen2.5-vl-7b",
        bbox=[0, 0, 10, 10],
        max_retries=2,
    )
    task_id = str(task["taskId"])
    assert mark_infer_task_processing(task_id, "infer-worker") is True
    QWEN_INFER_TASKS[task_id]["status"] = "CANCELED"

    assert mark_infer_task_completed(task_id, "infer-worker", {"text": "late"}) is False

    assert QWEN_INFER_TASKS[task_id]["status"] == "CANCELED"
    assert task_id not in QWEN_INFER_RESULTS
    assert get_finalize_task_for_item(item_id) is None


def test_canceled_qwen_finalize_task_ignores_late_worker_completion() -> None:
    job_id, item_id = _create_qwen_item("POSTPROCESSING")
    task = ensure_finalize_task(
        job_id=job_id,
        job_item_id=item_id,
        document_id="d_force_canceled_finalize",
        enqueue_now=False,
    )
    task_id = str(task["taskId"])
    QWEN_FINALIZE_TASKS[task_id]["status"] = "CANCELED"

    assert mark_finalize_task_completed(task_id, "finalize-worker", "out.txt") is False

    assert QWEN_FINALIZE_TASKS[task_id]["status"] == "CANCELED"
    assert QWEN_FINALIZE_TASKS[task_id].get("outputPath") == ""

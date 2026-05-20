from __future__ import annotations

import concurrent.futures
import socket
import time
from pathlib import Path
from threading import Event
from typing import Optional

from core.jobs.execution import QWEN_INFER_QUEUE_ROUTE
from core.jobs.qwen_stage_service import (
    all_infer_tasks_completed,
    all_infer_tasks_succeeded,
    count_infer_tasks,
    ensure_finalize_task,
    mark_infer_task_completed,
    mark_infer_task_failed,
    mark_infer_task_processing,
    mark_infer_task_retry,
)
from core.env import env_bool, env_first_int, env_str
from core.jobs.service import mark_item_final, mark_item_stage
from core.qwen_vl_client import get_qwen_vl_client, set_gpu_max_concurrent
from infra.queue.job_queue import ack as ack_message
from infra.queue.job_queue import dequeue_message, nack as nack_message
from infra.store import JOB_ITEMS, QWEN_INFER_TASKS
from worker.lease_heartbeat import LeaseHeartbeat
from worker.message_retry import requeue_missing_state_message


def _resolve_infer_worker_settings() -> tuple[int, int]:
    max_concurrency = env_first_int(
        (
            "QWEN_INFER_WORKER_MAX_CONCURRENCY",
            "QWEN_WORKER_MAX_CONCURRENCY",
            "WORKER_MAX_CONCURRENCY",
        ),
        1,
        minimum=1,
    )
    gpu_slots = env_first_int(
        (
            "QWEN_INFER_GPU_SLOTS",
            "GPU_MAX_CONCURRENT_INFERENCE",
        ),
        1,
        minimum=1,
    )
    return max_concurrency, gpu_slots


def _process_infer_message(message, *, effective_worker_id: str, client, poll_interval: float) -> None:
    task_id = message.item_id
    task = QWEN_INFER_TASKS.get(task_id)
    if task is None:
        requeue_missing_state_message(
            message=message,
            worker_id=effective_worker_id,
            reason="task missing",
            log_prefix="qwen-infer",
        )
        return

    item = JOB_ITEMS.get(str(task.get("jobItemId") or ""))
    if item is None:
        requeue_missing_state_message(
            message=message,
            worker_id=effective_worker_id,
            reason="job item missing",
            log_prefix="qwen-infer",
        )
        return

    if str(item.get("status") or "") in {"FAILED", "CANCELED", "COMPLETED"}:
        ack_message(message)
        return

    if not mark_infer_task_processing(task_id, effective_worker_id):
        current = QWEN_INFER_TASKS.get(task_id)
        if current and current.get("status") == "QUEUED":
            nack_message(message, requeue=True)
            time.sleep(poll_interval)
            return
        ack_message(message)
        return

    mark_item_stage(str(task["jobItemId"]), "GPU_PROCESSING", worker_id=effective_worker_id)
    retry_count = int(task.get("retryCount", 0))
    max_retries = int(task.get("maxRetries", 0))
    try:
        with LeaseHeartbeat(
            kind="qwen_infer_task",
            target_id=task_id,
            worker_id=effective_worker_id,
            job_id=str(task.get("jobId") or ""),
            job_item_id=str(task.get("jobItemId") or ""),
            metadata={"imageId": str(task.get("imageId") or "")},
        ):
            image_bytes = Path(str(task["imagePath"])).read_bytes()
            result = client.describe_image(
                image_bytes,
                language=str(task.get("language") or "ko"),
                is_table=task.get("isTable"),
                is_flowchart=task.get("isFlowchart"),
                is_math=task.get("isMath"),
            )
        mark_infer_task_completed(task_id, effective_worker_id, result)

        job_item_id = str(task["jobItemId"])
        if all_infer_tasks_completed(job_item_id) and all_infer_tasks_succeeded(job_item_id):
            ensure_finalize_task(
                job_id=str(task["jobId"]),
                job_item_id=job_item_id,
                document_id=str(task["documentId"]),
                enqueue_now=True,
            )
    except Exception as exc:  # noqa: BLE001
        error = {"code": "QWEN_INFER_FAIL", "message": str(exc)}
        if retry_count < max_retries:
            mark_infer_task_retry(task_id, error)
        else:
            mark_infer_task_failed(task_id, effective_worker_id, error)
            if count_infer_tasks(str(task["jobItemId"])) > 0:
                mark_item_final(str(task["jobItemId"]), "FAILED", error=error)
    finally:
        ack_message(message)


def run_qwen_infer_worker(
    *,
    worker_id: Optional[str] = None,
    poll_interval: float = 0.1,
    stop_event: Optional[Event] = None,
) -> None:
    effective_worker_id = worker_id or env_str("WORKER_ID") or socket.gethostname() or "qwen-infer-local-1"
    model_path = env_str(
        "QWEN_VL_7B_MODEL_PATH",
        "../models/Qwen2.5-VL-7B-Instruct",
    )
    device = env_str("VLM_DEVICE", "gpu")
    max_concurrency, gpu_slots = _resolve_infer_worker_settings()
    if max_concurrency < gpu_slots:
        print(
            f"[qwen-infer] worker concurrency ({max_concurrency}) is lower than gpu slots ({gpu_slots}); "
            "some slots may remain idle"
        )

    set_gpu_max_concurrent(gpu_slots)
    client = get_qwen_vl_client(model_path=model_path, device=device)
    if env_bool("QWEN_WARMUP_ON_START", False):
        started_at = time.time()
        print("[qwen-infer] warming up qwen model")
        client.ensure_model_loaded()
        print(f"[qwen-infer] qwen model warmup completed in {time.time() - started_at:.2f}s")

    print(
        f"[qwen-infer] started: {effective_worker_id}, route={QWEN_INFER_QUEUE_ROUTE}, "
        f"model_path={model_path}, device={device}, max_concurrency={max_concurrency}, gpu_slots={gpu_slots}"
    )
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_concurrency) as executor:
        in_flight: dict[concurrent.futures.Future[None], object] = {}
        while True:
            if stop_event and stop_event.is_set():
                print(f"[qwen-infer] stopped: {effective_worker_id}")
                return

            while len(in_flight) < max_concurrency:
                message = dequeue_message(queue_route=QWEN_INFER_QUEUE_ROUTE)
                if message is None:
                    break
                future = executor.submit(
                    _process_infer_message,
                    message,
                    effective_worker_id=effective_worker_id,
                    client=client,
                    poll_interval=poll_interval,
                )
                in_flight[future] = message

            if not in_flight:
                time.sleep(poll_interval)
                continue

            done, _pending = concurrent.futures.wait(
                set(in_flight.keys()),
                timeout=poll_interval,
                return_when=concurrent.futures.FIRST_COMPLETED,
            )
            for future in done:
                message = in_flight.pop(future, None)
                try:
                    future.result()
                except Exception as exc:  # noqa: BLE001
                    print(f"[qwen-infer] unhandled task exception: {exc}")
                    if message is not None:
                        nack_message(message, requeue=True)

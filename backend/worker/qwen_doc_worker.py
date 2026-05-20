from __future__ import annotations

import socket
import time
from threading import Event
from typing import Optional

from infra.storage.file_assets import materialize_record_asset
from core.jobs.execution import QWEN_DOC_QUEUE_ROUTE
from core.jobs.qwen_stage_service import (
    create_infer_task,
    ensure_finalize_task,
    infer_task_id_for,
    prune_infer_tasks,
    reset_finalize_task_for_item,
    save_preprocess_payload,
)
from core.config import load_config
from core.env import env_int, env_str
from core.jobs.service import mark_item_final, mark_item_processing, mark_item_retry, mark_item_stage, next_item_message
from core.qwen_staged_pipeline import cleanup_staged_artifacts, preprocess_document_for_qwen
from infra.queue.job_queue import ack as ack_message
from infra.queue.job_queue import nack as nack_message
from infra.store import JOB_ITEMS, JOBS
from worker.lease_heartbeat import LeaseHeartbeat
from worker.message_retry import requeue_missing_state_message


def run_qwen_doc_worker(
    *,
    worker_id: Optional[str] = None,
    poll_interval: float = 0.2,
    stop_event: Optional[Event] = None,
) -> None:
    effective_worker_id = worker_id or env_str("WORKER_ID") or socket.gethostname() or "qwen-doc-local-1"
    config = load_config()
    infer_max_retries = env_int("QWEN_INFER_MAX_RETRIES", 2, minimum=0)

    print(f"[qwen-doc] started: {effective_worker_id}, route={QWEN_DOC_QUEUE_ROUTE}")
    while True:
        if stop_event and stop_event.is_set():
            print(f"[qwen-doc] stopped: {effective_worker_id}")
            return

        message = next_item_message(queue_route=QWEN_DOC_QUEUE_ROUTE)
        if message is None:
            time.sleep(poll_interval)
            continue

        should_ack = True
        item_id = message.item_id
        item = JOB_ITEMS.get(item_id)
        if item is None:
            requeue_missing_state_message(
                message=message,
                worker_id=effective_worker_id,
                reason="item missing",
                log_prefix="qwen-doc",
            )
            continue

        job = JOBS.get(str(item.get("jobId") or ""))
        if job is None:
            requeue_missing_state_message(
                message=message,
                worker_id=effective_worker_id,
                reason="job missing",
                log_prefix="qwen-doc",
            )
            continue

        if job.get("cancelRequested"):
            mark_item_final(item_id, "CANCELED", error={"code": "JOB_CANCELED", "message": "job canceled"})
            ack_message(message)
            continue

        if not mark_item_processing(item_id, effective_worker_id, stage="PREPROCESSING"):
            current = JOB_ITEMS.get(item_id)
            if current and current.get("status") == "QUEUED":
                nack_message(message, requeue=True)
                time.sleep(poll_interval)
                continue
            ack_message(message)
            continue

        item = JOB_ITEMS.get(item_id)
        if item is None:
            requeue_missing_state_message(
                message=message,
                worker_id=effective_worker_id,
                reason="item disappeared after claim",
                log_prefix="qwen-doc",
            )
            continue

        retry_count = int(item.get("retryCount", 0))
        max_retries = int(item.get("maxRetries", 3))
        try:
            with LeaseHeartbeat(
                kind="qwen_doc_item",
                target_id=item_id,
                worker_id=effective_worker_id,
                job_id=str(item["jobId"]),
                job_item_id=item_id,
                metadata={"stage": "PREPROCESSING"},
            ):
                cleanup_staged_artifacts(tmp_root=config.tmp_root, job_item_id=item_id)
                with materialize_record_asset(
                    item,
                    file_id_key="sourceFileId",
                    filename_key="sourceFilename",
                    fallback_path_key="sourcePath",
                    tmp_root=config.tmp_root,
                    purpose="qwen_doc_source",
                    owner_id=item_id,
                ) as source_path:
                    payload = preprocess_document_for_qwen(
                        source_path=source_path,
                        job_item_id=item_id,
                        config=config,
                    )
                save_preprocess_payload(item_id, payload)
                reset_finalize_task_for_item(item_id, error=None, enqueue_now=False)

                infer_inputs = list(payload.get("inferenceInputs") or [])
                desired_task_ids: set[str] = set()
                for infer_input in infer_inputs:
                    image_id = str(infer_input.get("imageId") or "")
                    desired_task_ids.add(infer_task_id_for(item_id, image_id))
                    create_infer_task(
                        job_id=str(item["jobId"]),
                        job_item_id=item_id,
                        document_id=str(item["documentId"]),
                        page_num=int(infer_input.get("pageNum", 1)),
                        image_id=image_id,
                        image_path=str(infer_input.get("imagePath") or ""),
                        language=str(item.get("language") or "ko"),
                        model_code=str(item.get("modelCode") or "qwen2.5-vl-7b"),
                        bbox=list(infer_input.get("bbox") or [0, 0, 0, 0]),
                        is_table=infer_input.get("isTable"),
                        is_flowchart=infer_input.get("isFlowchart"),
                        is_math=infer_input.get("isMath"),
                        max_retries=infer_max_retries,
                        requeue_existing=retry_count > 0,
                    )
                prune_infer_tasks(item_id, desired_task_ids)

                mark_item_stage(item_id, "GPU_WAITING", worker_id=effective_worker_id)
                if not infer_inputs:
                    ensure_finalize_task(
                        job_id=str(item["jobId"]),
                        job_item_id=item_id,
                        document_id=str(item["documentId"]),
                        enqueue_now=True,
                    )
        except Exception as exc:  # noqa: BLE001
            error = {"code": "QWEN_PREPROCESS_FAIL", "message": str(exc)}
            if retry_count < max_retries:
                mark_item_retry(item_id, error=error)
            else:
                mark_item_final(item_id, "FAILED", error=error)
        finally:
            if should_ack:
                ack_message(message)

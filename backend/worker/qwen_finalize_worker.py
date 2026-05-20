from __future__ import annotations

import socket
import time
from threading import Event
from typing import Optional

from core.documents.storage import (
    cleanup_output_artifacts,
    persist_result_artifact,
    result_filename_for,
)
from core.jobs.execution import QWEN_FINALIZE_QUEUE_ROUTE
from core.jobs.qwen_stage_service import (
    all_infer_tasks_completed,
    all_infer_tasks_succeeded,
    get_finalize_task,
    get_preprocess_payload,
    list_infer_results,
    mark_finalize_task_completed,
    mark_finalize_task_failed,
    mark_finalize_task_processing,
)
from core.config import load_config
from core.env import env_str
from core.jobs.service import mark_item_final, mark_item_stage
from core.qwen_staged_pipeline import cleanup_staged_artifacts, finalize_document_from_qwen_results
from infra.queue.job_queue import ack as ack_message
from infra.queue.job_queue import dequeue_message, nack as nack_message
from infra.storage.file_assets import materialize_record_asset
from infra.storage.settings import get_configured_storage_path
from infra.store import DOCUMENTS, JOB_ITEMS
from worker.lease_heartbeat import LeaseHeartbeat
from worker.message_retry import requeue_missing_state_message


def _cleanup_staged_artifacts(config, job_item_id: str) -> None:
    try:
        cleanup_staged_artifacts(tmp_root=config.tmp_root, job_item_id=job_item_id)
    except Exception as exc:  # noqa: BLE001
        print(f"[qwen-finalize] staged artifact cleanup skipped for {job_item_id}: {exc}")


def run_qwen_finalize_worker(
    *,
    worker_id: Optional[str] = None,
    poll_interval: float = 0.2,
    stop_event: Optional[Event] = None,
) -> None:
    effective_worker_id = worker_id or env_str("WORKER_ID") or socket.gethostname() or "qwen-finalize-local-1"
    config = load_config()

    print(f"[qwen-finalize] started: {effective_worker_id}, route={QWEN_FINALIZE_QUEUE_ROUTE}")
    while True:
        if stop_event and stop_event.is_set():
            print(f"[qwen-finalize] stopped: {effective_worker_id}")
            return

        message = dequeue_message(queue_route=QWEN_FINALIZE_QUEUE_ROUTE)
        if message is None:
            time.sleep(poll_interval)
            continue

        task_id = message.item_id
        task = get_finalize_task(task_id)
        if task is None:
            requeue_missing_state_message(
                message=message,
                worker_id=effective_worker_id,
                reason="finalize task missing",
                log_prefix="qwen-finalize",
            )
            continue

        item = JOB_ITEMS.get(str(task.get("jobItemId") or ""))
        if item is None:
            requeue_missing_state_message(
                message=message,
                worker_id=effective_worker_id,
                reason="job item missing",
                log_prefix="qwen-finalize",
            )
            continue

        item_status = str(item.get("status") or "")
        if item_status in {"FAILED", "CANCELED", "COMPLETED"}:
            _cleanup_staged_artifacts(config, str(task.get("jobItemId") or ""))
            ack_message(message)
            continue

        if not all_infer_tasks_completed(str(task["jobItemId"])):
            nack_message(message, requeue=True)
            time.sleep(max(poll_interval, 0.5))
            continue

        if not all_infer_tasks_succeeded(str(task["jobItemId"])):
            error = {"code": "QWEN_INFER_FAILED", "message": "one or more qwen inference tasks failed"}
            mark_finalize_task_failed(task_id, effective_worker_id, error)
            mark_item_final(str(task["jobItemId"]), "FAILED", error=error)
            _cleanup_staged_artifacts(config, str(task["jobItemId"]))
            ack_message(message)
            continue

        if not mark_finalize_task_processing(task_id, effective_worker_id):
            current = get_finalize_task(task_id)
            if current and current.get("status") == "QUEUED":
                nack_message(message, requeue=True)
                time.sleep(poll_interval)
                continue
            ack_message(message)
            continue

        mark_item_stage(str(task["jobItemId"]), "POSTPROCESSING", worker_id=effective_worker_id)
        try:
            with LeaseHeartbeat(
                kind="qwen_finalize_task",
                target_id=task_id,
                worker_id=effective_worker_id,
                job_id=str(task.get("jobId") or ""),
                job_item_id=str(task.get("jobItemId") or ""),
                metadata={"documentId": str(task.get("documentId") or "")},
            ):
                preprocess_payload = get_preprocess_payload(str(task["jobItemId"]))
                if preprocess_payload is None:
                    raise RuntimeError(f"missing preprocess payload for {task['jobItemId']}")

                doc = DOCUMENTS.get(str(task.get("documentId") or ""))
                with materialize_record_asset(
                    item,
                    file_id_key="sourceFileId",
                    filename_key="sourceFilename",
                    fallback_path_key="sourcePath",
                    tmp_root=config.tmp_root,
                    purpose="qwen_finalize_source",
                    owner_id=str(task["jobItemId"]),
                ) as source_path:
                    output_dir = config.tmp_root / "qwen_finalize" / str(task["jobItemId"])
                    output_dir.mkdir(parents=True, exist_ok=True)
                    output_path = output_dir / result_filename_for(
                        str(item.get("sourceFilename") or item.get("fileName") or source_path.name)
                    )
                    infer_results = list_infer_results(str(task["jobItemId"]))
                    final_path = finalize_document_from_qwen_results(
                        source_path=source_path,
                        output_path=output_path,
                        preprocess_payload=preprocess_payload,
                        infer_results=infer_results,
                    )
                    stored_output = persist_result_artifact(
                        document_id=str(task.get("documentId") or ""),
                        output_path=final_path,
                        output_filename=output_path.name,
                        display_source=str(
                            item.get("sourceFilename")
                            or (doc or {}).get("sourceFilename")
                            or (doc or {}).get("originalFilename")
                            or item.get("sourcePath")
                            or ""
                        ),
                        display_pdf_path=(
                            str((doc or {}).get("originalFilePath") or "")
                            if str((doc or {}).get("fileType") or "").lower() == "pdf"
                            else None
                        ),
                        storage_root=get_configured_storage_path(config.output_root),
                    )
                    cleanup_output_artifacts(final_path)
            mark_finalize_task_completed(task_id, effective_worker_id, str(stored_output["storagePath"]))
            mark_item_final(
                str(task["jobItemId"]),
                "COMPLETED",
                output_path=str(stored_output["storagePath"]),
                output_file_id=str(stored_output["fileId"]),
                meta=dict(stored_output["meta"]),
            )
            _cleanup_staged_artifacts(config, str(task["jobItemId"]))
        except Exception as exc:  # noqa: BLE001
            error = {"code": "QWEN_FINALIZE_FAIL", "message": str(exc)}
            mark_finalize_task_failed(task_id, effective_worker_id, error)
            mark_item_final(str(task["jobItemId"]), "FAILED", error=error)
            _cleanup_staged_artifacts(config, str(task["jobItemId"]))
        finally:
            ack_message(message)

import concurrent.futures
import multiprocessing as mp
import queue
import socket
import time
from pathlib import Path
from threading import Event
from typing import Any, Callable, Literal, Optional

from core.config import load_config
from core.documents.storage import (
    cleanup_output_artifacts,
    persist_result_artifact,
    result_filename_for,
)
from core.env import env_float, env_int, env_str
from core.jobs.execution import worker_queue_routes
from core.jobs.service import (
    mark_item_final,
    mark_item_processing,
    mark_item_progress,
    mark_item_retry,
    next_item_message,
)
from core.pipeline import DocumentPipeline
from infra.queue.job_queue import ack as ack_message
from infra.queue.job_queue import enqueue as enqueue_message
from infra.queue.job_queue import nack as nack_message
from infra.queue.message import QueueMessage
from infra.storage.file_assets import materialize_record_asset
from infra.storage.settings import get_configured_storage_path
from infra.store import DOCUMENTS, JOB_ITEMS, JOBS
from worker.message_retry import requeue_missing_state_message

runtime_config = load_config()


ProcessAction = Literal["ack", "nack", "bounded_requeue"]
ProcessResult = tuple[ProcessAction, str]
ProgressCallback = Callable[[int, int], None]


def _coerce_progress_numbers(args: tuple[Any, ...]) -> tuple[int, int]:
    if len(args) >= 3:
        current, total = args[1], args[2]
    elif len(args) >= 2:
        current, total = args[0], args[1]
    else:
        return 0, 0

    try:
        current_int = int(current or 0)
    except (TypeError, ValueError):
        current_int = 0
    try:
        total_int = int(total or 0)
    except (TypeError, ValueError):
        total_int = 0
    return max(0, current_int), max(0, total_int)


def _run_pipeline_in_subprocess(source_path: str, language: str, model_code: Optional[str], result_queue) -> None:
    try:
        child_config = load_config()
        child_pipeline = DocumentPipeline(child_config)

        if model_code:
            child_pipeline.update_vlm_model(model_code)

        def _progress_callback(*args: Any) -> None:
            current, total = _coerce_progress_numbers(args)
            result_queue.put(
                {
                    "type": "progress",
                    "currentPage": current,
                    "totalPages": total,
                }
            )

        output_path = child_pipeline.process_file(
            Path(source_path),
            language,
            progress_callback=_progress_callback,
        )
        result_queue.put({"type": "result", "ok": True, "outputPath": str(output_path)})
    except Exception as exc:  # noqa: BLE001
        result_queue.put({"type": "result", "ok": False, "error": str(exc)})


def _drain_process_messages(result_queue, progress_callback: ProgressCallback | None) -> dict[str, Any] | None:
    final_result: dict[str, Any] | None = None
    while True:
        try:
            message = result_queue.get_nowait()
        except queue.Empty:
            break

        if not isinstance(message, dict):
            continue
        if message.get("type") == "progress":
            if progress_callback is not None:
                progress_callback(
                    int(message.get("currentPage") or 0),
                    int(message.get("totalPages") or 0),
                )
            continue
        if message.get("type") == "result":
            final_result = message
    return final_result


def _process(
    path: Path,
    language: str,
    model_code: Optional[str],
    *,
    progress_callback: ProgressCallback | None = None,
) -> Path:
    ctx = mp.get_context("spawn")
    result_queue = ctx.Queue()
    process = ctx.Process(
        target=_run_pipeline_in_subprocess,
        args=(str(path), language, model_code, result_queue),
        daemon=True,
    )
    process.start()
    final_result: dict[str, Any] | None = None
    while process.is_alive():
        process.join(timeout=0.1)
        drained_result = _drain_process_messages(result_queue, progress_callback)
        if drained_result is not None:
            final_result = drained_result
    process.join()
    drained_result = _drain_process_messages(result_queue, progress_callback)
    if drained_result is not None:
        final_result = drained_result

    if process.exitcode not in (0, None):
        raise RuntimeError(f"processing worker exited abnormally (exitcode={process.exitcode})")

    if final_result is None:
        raise RuntimeError("processing worker returned no result")

    if not final_result.get("ok"):
        raise RuntimeError(str(final_result.get("error") or "processing failed"))
    return Path(str(final_result.get("outputPath")))


def _requeue_if_still_queued(item_id: str) -> None:
    current = JOB_ITEMS.get(item_id)
    if current and current.get("status") == "QUEUED":
        enqueue_message(
            item_id,
            job_id=current.get("jobId"),
            attempt=int(current.get("retryCount", 0)),
            queued_at=current.get("queuedAt"),
            queue_route=current.get("queueRoute"),
        )


def _process_item(message: QueueMessage, worker_id: str) -> ProcessResult:
    item_id = message.item_id
    item = JOB_ITEMS.get(item_id)
    if not item:
        return ("bounded_requeue", "item missing")

    job = JOBS.get(item["jobId"])
    if not job:
        return ("bounded_requeue", f"job missing: job_id={item.get('jobId')}")

    if job.get("cancelRequested"):
        mark_item_final(item_id, "CANCELED", error={"code": "JOB_CANCELED", "message": "job canceled"})
        return ("ack", "")

    if not mark_item_processing(item_id, worker_id):
        current = JOB_ITEMS.get(item_id)
        if current and current.get("status") == "QUEUED":
            print(f"[worker] failed to claim queued item; will requeue: item_id={item_id}, worker_id={worker_id}")
            return ("nack", "failed to claim queued item")
        return ("ack", "")

    item = JOB_ITEMS.get(item_id)
    if not item:
        return ("bounded_requeue", "item disappeared after claim")

    max_retries = int(item.get("maxRetries", 3))
    retry_count = int(item.get("retryCount", 0))
    model_code = item.get("modelCode")
    doc = DOCUMENTS.get(str(item.get("documentId") or ""))

    try:
        with materialize_record_asset(
            item,
            file_id_key="sourceFileId",
            filename_key="sourceFilename",
            fallback_path_key="sourcePath",
            tmp_root=runtime_config.tmp_root,
            purpose="worker_source",
            owner_id=item_id,
        ) as source_path:
            def _handle_progress(current: int, total: int) -> None:
                mark_item_progress(
                    item_id,
                    current,
                    total,
                    worker_id=worker_id,
                )

            out_path = _process(
                source_path,
                item.get("language", "ko"),
                str(model_code) if model_code else None,
                progress_callback=_handle_progress,
            )
            stored_output = persist_result_artifact(
                document_id=str(item.get("documentId") or ""),
                output_path=out_path,
                output_filename=result_filename_for(
                    str(item.get("sourceFilename") or item.get("fileName") or source_path.name)
                ),
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
                storage_root=get_configured_storage_path(runtime_config.output_root),
            )
            cleanup_output_artifacts(out_path)
        mark_item_final(
            item_id,
            "COMPLETED",
            output_path=str(stored_output["storagePath"]),
            output_file_id=str(stored_output["fileId"]),
            meta=dict(stored_output["meta"]),
        )
    except Exception as exc:  # noqa: BLE001
        error = {"code": "CONVERSION_FAIL", "message": str(exc)}
        if retry_count < max_retries:
            mark_item_retry(item_id, error=error)
        else:
            mark_item_final(item_id, "FAILED", error=error)
    return ("ack", "")


def _dequeue_next_message(queue_routes: list[str], cursor: list[int]) -> Optional[QueueMessage]:
    if not queue_routes:
        return None

    route_count = len(queue_routes)
    start_index = cursor[0] % route_count
    for offset in range(route_count):
        index = (start_index + offset) % route_count
        route = queue_routes[index]
        message = next_item_message(queue_route=route)
        cursor[0] = (index + 1) % route_count
        if message is not None:
            if not message.queue_route:
                message.queue_route = route
            return message
    return None


def run_worker(
    worker_id: str = "worker-local-1",
    poll_interval: float = 0.2,
    stop_event: Optional[Event] = None,
    max_concurrency: Optional[int] = None,
    worker_mode: Optional[str] = None,
    queue_routes: Optional[list[str]] = None,
) -> None:
    if max_concurrency is None:
        max_concurrency = env_int("WORKER_MAX_CONCURRENCY", 1)
    max_concurrency = max(1, int(max_concurrency))

    if queue_routes is None:
        queue_routes = worker_queue_routes(worker_mode or env_str("WORKER_MODE", "all"))
    queue_routes = [route for route in queue_routes if route]
    if not queue_routes:
        raise ValueError("worker queue routes must not be empty")

    effective_mode = worker_mode or env_str("WORKER_MODE", "all")
    print(
        f"[worker] started: {worker_id}, mode={effective_mode}, "
        f"routes={','.join(queue_routes)}, max_concurrency={max_concurrency}"
    )

    dequeue_cursor = [0]
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_concurrency) as executor:
        in_flight: dict[concurrent.futures.Future[None], QueueMessage] = {}
        while True:
            if stop_event and stop_event.is_set():
                for message in in_flight.values():
                    nack_message(message, requeue=True)
                for future in in_flight:
                    future.cancel()
                print(f"[worker] stopped: {worker_id}")
                return

            while len(in_flight) < max_concurrency:
                try:
                    message = _dequeue_next_message(queue_routes, dequeue_cursor)
                except Exception as exc:  # noqa: BLE001
                    print(f"[worker] dequeue failed: {exc}")
                    time.sleep(max(poll_interval, 1.0))
                    break
                if not message:
                    break
                future = executor.submit(_process_item, message, worker_id)
                in_flight[future] = message

            if not in_flight:
                time.sleep(poll_interval)
                continue

            done, pending = concurrent.futures.wait(
                set(in_flight.keys()),
                timeout=poll_interval,
                return_when=concurrent.futures.FIRST_COMPLETED,
            )
            for future in done:
                message = in_flight.pop(future, None)
                try:
                    action, reason = future.result()
                except Exception as exc:  # noqa: BLE001
                    print(f"[worker] unhandled item exception: {exc}")
                    if message is not None:
                        nack_message(message, requeue=True)
                    continue

                if message is None:
                    continue

                if action == "bounded_requeue":
                    requeue_missing_state_message(
                        message=message,
                        worker_id=worker_id,
                        reason=reason,
                        log_prefix="worker",
                    )
                    continue

                if action == "nack":
                    nack_message(message, requeue=True)
                    continue

                try:
                    ack_message(message)
                except Exception as exc:  # noqa: BLE001
                    print(f"[worker] ack failed: {exc}")


def run_worker_from_env() -> None:
    worker_id = (env_str("WORKER_ID") or socket.gethostname() or "worker-local-1").strip()
    worker_mode = env_str("WORKER_MODE", "all", strip=True)
    poll_interval = env_float("WORKER_POLL_INTERVAL_SECONDS", 0.2, minimum=0.05)
    max_concurrency = env_int("WORKER_MAX_CONCURRENCY", 1, minimum=1)

    run_worker(
        worker_id=worker_id,
        poll_interval=poll_interval,
        max_concurrency=max_concurrency,
        worker_mode=worker_mode,
    )

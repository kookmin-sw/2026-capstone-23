from datetime import datetime, timedelta, timezone
import importlib
import os
import sys
import types
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("QUEUE_BACKEND", "memory")
os.environ.setdefault("STORE_BACKEND", "sqlite")
os.environ.setdefault("STATUS_CACHE_BACKEND", "none")

from core.jobs.service import (
    cleanup_old_job_events,
    create_job,
    create_job_item,
    mark_item_processing,
    mark_item_progress,
    mark_item_stage,
    set_job_progress_publisher,
)
from api.ws_hub import publish_job_progress
from infra.queue.job_queue import QUEUE
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
from storage.sqlite_files import clear_file_blobs


def make_client(tmp_path: Path) -> TestClient:
    deps_module = types.ModuleType("api.dependencies")
    deps_module.config = SimpleNamespace(
        input_root=tmp_path / "inputs",
        output_root=tmp_path / "outputs",
        tmp_root=tmp_path / "tmp",
        openai_model="gpt-5.2",
    )
    deps_module.pipeline = SimpleNamespace(
        update_vlm_model=lambda *_args, **_kwargs: None,
        process_file=lambda *_args, **_kwargs: tmp_path / "outputs" / "dummy.txt",
    )

    original_dependencies = sys.modules.get("api.dependencies")
    try:
        sys.modules["api.dependencies"] = deps_module
        import api.routers.parser as parser_module

        parser_module = importlib.reload(parser_module)
    finally:
        if original_dependencies is None:
            sys.modules.pop("api.dependencies", None)
        else:
            sys.modules["api.dependencies"] = original_dependencies

    app = FastAPI()
    app.include_router(parser_module.router, prefix="/api/v1")
    return TestClient(app)


def setup_function():
    JOBS.clear()
    JOB_ITEMS.clear()
    JOB_EVENTS.clear()
    DOCUMENTS.clear()
    QWEN_INFER_TASKS.clear()
    QWEN_INFER_RESULTS.clear()
    QWEN_FINALIZE_TASKS.clear()
    WORKER_LEASES.clear()
    clear_file_blobs()
    for route in (None, "openai", "openrouter", "qwen_gpu", "qwen_doc", "qwen_infer", "qwen_finalize"):
        while True:
            item_id = QUEUE.dequeue(queue_route=route)
            if item_id is None:
                break


def test_parser_jobs_create_get_items_and_cancel(tmp_path: Path):
    client = make_client(tmp_path)

    create_res = client.post(
        "/api/v1/parser/jobs",
        data={
            "userId": "u-test",
            "modelId": "m1",
            "parallelism": "1",
            "language": "ko",
        },
        files=[("files", ("sample.txt", b"hello", "text/plain"))],
    )
    assert create_res.status_code == 200
    created = create_res.json()["data"]
    job_id = created["jobId"]

    assert created["status"] == "QUEUED"
    assert created["totalItems"] == 1
    assert created["requestedExecutionBackend"] == "auto"
    assert created["executionBackend"] == "openrouter"
    assert created["queueRoute"] == "openrouter"
    assert job_id in JOBS

    job_res = client.get(f"/api/v1/parser/jobs/{job_id}")
    assert job_res.status_code == 200
    job = job_res.json()["data"]
    assert job["jobId"] == job_id
    assert job["status"] == "QUEUED"
    assert job["queuedItems"] == 1
    assert job["executionBackend"] == "openrouter"

    items_res = client.get(f"/api/v1/parser/jobs/{job_id}/items")
    assert items_res.status_code == 200
    items = items_res.json()["data"]["items"]
    assert len(items) == 1
    assert items[0]["jobId"] == job_id
    assert items[0]["status"] == "QUEUED"
    assert items[0]["modelCode"] == "openrouter/openai/gpt-5.2"
    assert items[0]["executionBackend"] == "openrouter"
    assert items[0]["queueRoute"] == "openrouter"
    assert items[0]["queueWaitMs"] is None
    assert items[0]["processingTimeMs"] is None
    assert items[0]["endToEndMs"] is None
    assert [event["eventType"] for event in items[0]["events"]] == ["CREATED"]
    job_item_id = items[0]["jobItemId"]

    job_events_res = client.get(f"/api/v1/parser/jobs/{job_id}/events")
    assert job_events_res.status_code == 200
    job_events = job_events_res.json()["data"]["events"]
    assert [event["eventType"] for event in job_events] == ["CREATED", "CREATED"]

    item_events_res = client.get(f"/api/v1/parser/jobs/{job_id}/events", params={"jobItemId": job_item_id})
    assert item_events_res.status_code == 200
    item_events = item_events_res.json()["data"]["events"]
    assert [event["eventType"] for event in item_events] == ["CREATED"]

    cancel_res = client.post(f"/api/v1/parser/jobs/{job_id}/cancel")
    assert cancel_res.status_code == 200
    cancel_data = cancel_res.json()["data"]
    assert cancel_data["status"] == "CANCELING"

    job_after_cancel = client.get(f"/api/v1/parser/jobs/{job_id}").json()["data"]
    assert job_after_cancel["cancelRequested"] is True
    assert job_after_cancel["canceledItems"] == 1

    items_after_cancel = client.get(f"/api/v1/parser/jobs/{job_id}/items").json()["data"]["items"]
    assert len(items_after_cancel) == 1
    assert items_after_cancel[0]["status"] == "CANCELED"
    assert items_after_cancel[0]["queueWaitMs"] is None
    assert items_after_cancel[0]["processingTimeMs"] is None
    assert items_after_cancel[0]["endToEndMs"] is not None
    assert [event["eventType"] for event in items_after_cancel[0]["events"]] == ["CREATED", "CANCELED"]

    job_events_after_cancel = client.get(f"/api/v1/parser/jobs/{job_id}/events").json()["data"]["events"]
    assert [event["eventType"] for event in job_events_after_cancel] == ["CREATED", "CREATED", "CANCELED"]


def test_parser_jobs_force_cancel_marks_processing_qwen_state_final(tmp_path: Path):
    client = make_client(tmp_path)
    job_id = create_job(
        model_id="m3",
        parallelism=1,
        total_items=2,
        requested_execution_backend="qwen_gpu",
        execution_backend="qwen_gpu",
    )
    first_item_id = create_job_item(
        job_id=job_id,
        document_id="d_force_1",
        file_name="one.pdf",
        source_path="one.pdf",
        language="ko",
        execution_backend="qwen_gpu",
        queue_route="qwen_doc",
        model_code="qwen2.5-vl-32b",
    )
    second_item_id = create_job_item(
        job_id=job_id,
        document_id="d_force_2",
        file_name="two.pdf",
        source_path="two.pdf",
        language="ko",
        execution_backend="qwen_gpu",
        queue_route="qwen_doc",
        model_code="qwen2.5-vl-32b",
    )
    assert mark_item_processing(first_item_id, "worker-1", stage="GPU_PROCESSING") is True
    QWEN_INFER_TASKS["qi_force"] = {
        "taskId": "qi_force",
        "jobId": job_id,
        "jobItemId": first_item_id,
        "status": "PROCESSING",
    }
    QWEN_INFER_RESULTS["qi_force"] = {"taskId": "qi_force", "jobItemId": first_item_id}
    QWEN_FINALIZE_TASKS["qf_force"] = {
        "taskId": "qf_force",
        "jobId": job_id,
        "jobItemId": first_item_id,
        "status": "QUEUED",
    }
    WORKER_LEASES["qwen_infer_task:qi_force"] = {
        "leaseId": "qwen_infer_task:qi_force",
        "jobId": job_id,
        "jobItemId": first_item_id,
    }

    cancel_res = client.post(f"/api/v1/parser/jobs/{job_id}/cancel", params={"force": "true"})

    assert cancel_res.status_code == 200
    data = cancel_res.json()["data"]
    assert data["force"] is True
    assert data["cancelAppliedItems"] == 2
    assert JOBS[job_id]["status"] == "CANCELED"
    assert JOB_ITEMS[first_item_id]["status"] == "CANCELED"
    assert JOB_ITEMS[second_item_id]["status"] == "CANCELED"
    assert QWEN_INFER_TASKS["qi_force"]["status"] == "CANCELED"
    assert "qi_force" not in QWEN_INFER_RESULTS
    assert QWEN_FINALIZE_TASKS["qf_force"]["status"] == "CANCELED"
    assert WORKER_LEASES == {}


def test_parser_jobs_can_route_qwen_gpu_work_to_separate_queue(tmp_path: Path):
    client = make_client(tmp_path)

    create_res = client.post(
        "/api/v1/parser/jobs",
        data={
            "userId": "u-test",
            "modelId": "m3",
            "parallelism": "1",
            "executionBackend": "qwen_gpu",
            "language": "ko",
        },
        files=[("files", ("sample.pdf", b"%PDF-1.7\nhello", "application/pdf"))],
    )
    assert create_res.status_code == 200

    created = create_res.json()["data"]
    assert created["requestedExecutionBackend"] == "qwen_gpu"
    assert created["executionBackend"] == "qwen_gpu"
    assert created["queueRoute"] == "qwen_doc"

    job_id = created["jobId"]
    items = client.get(f"/api/v1/parser/jobs/{job_id}/items").json()["data"]["items"]
    assert items[0]["executionBackend"] == "qwen_gpu"
    assert items[0]["queueRoute"] == "qwen_doc"


def test_parser_jobs_reject_incompatible_model_and_execution_backend(tmp_path: Path):
    client = make_client(tmp_path)

    create_res = client.post(
        "/api/v1/parser/jobs",
        data={
            "userId": "u-test",
            "modelId": "m1",
            "parallelism": "1",
            "executionBackend": "qwen_gpu",
            "language": "ko",
        },
        files=[("files", ("sample.pdf", b"%PDF-1.7\nhello", "application/pdf"))],
    )
    assert create_res.status_code == 422


def test_parser_jobs_auto_routes_local_models_to_qwen_gpu(tmp_path: Path):
    client = make_client(tmp_path)

    create_res = client.post(
        "/api/v1/parser/jobs",
        data={
            "userId": "u-test",
            "modelId": "m3",
            "parallelism": "1",
            "executionBackend": "auto",
            "language": "ko",
        },
        files=[("files", ("sample.pdf", b"%PDF-1.7\nhello", "application/pdf"))],
    )
    assert create_res.status_code == 200

    created = create_res.json()["data"]
    assert created["requestedExecutionBackend"] == "auto"
    assert created["executionBackend"] == "qwen_gpu"
    assert created["queueRoute"] == "qwen_doc"


def test_parser_jobs_routes_gpt5_mini_to_openrouter_queue(tmp_path: Path):
    client = make_client(tmp_path)

    create_res = client.post(
        "/api/v1/parser/jobs",
        data={
            "userId": "u-test",
            "modelId": "m2",
            "parallelism": "1",
            "executionBackend": "auto",
            "language": "ko",
        },
        files=[("files", ("sample.pdf", b"%PDF-1.7\nhello", "application/pdf"))],
    )
    assert create_res.status_code == 200

    created = create_res.json()["data"]
    assert created["executionBackend"] == "openrouter"
    assert created["queueRoute"] == "openrouter"


def test_parser_jobs_auto_routes_openrouter_models_to_openrouter_queue(tmp_path: Path):
    client = make_client(tmp_path)

    create_res = client.post(
        "/api/v1/parser/jobs",
        data={
            "userId": "u-test",
            "modelId": "m4",
            "parallelism": "1",
            "executionBackend": "auto",
            "language": "ko",
        },
        files=[("files", ("sample.pdf", b"%PDF-1.7\nhello", "application/pdf"))],
    )
    assert create_res.status_code == 200

    created = create_res.json()["data"]
    assert created["requestedExecutionBackend"] == "auto"
    assert created["executionBackend"] == "openrouter"
    assert created["queueRoute"] == "openrouter"


def test_job_parallelism_limit_is_enforced_per_job() -> None:
    job_id = create_job(
        model_id="m1",
        parallelism=1,
        total_items=2,
        requested_execution_backend="auto",
        execution_backend="openai",
    )
    first_item_id = create_job_item(
        job_id=job_id,
        document_id="d_1",
        file_name="one.txt",
        source_path="one.txt",
        language="ko",
        execution_backend="openai",
        queue_route="openai",
        model_code="gpt-5.2",
    )
    second_item_id = create_job_item(
        job_id=job_id,
        document_id="d_2",
        file_name="two.txt",
        source_path="two.txt",
        language="ko",
        execution_backend="openai",
        queue_route="openai",
        model_code="gpt-5.2",
    )

    assert mark_item_processing(first_item_id, "worker-1") is True
    assert mark_item_processing(second_item_id, "worker-2") is False
    assert JOB_ITEMS[first_item_id]["status"] == "PROCESSING"
    assert JOB_ITEMS[second_item_id]["status"] == "QUEUED"


def test_qwen_preprocessing_parallelism_ignores_items_waiting_for_gpu() -> None:
    job_id = create_job(
        model_id="m3",
        parallelism=1,
        total_items=2,
        requested_execution_backend="qwen_gpu",
        execution_backend="qwen_gpu",
    )
    first_item_id = create_job_item(
        job_id=job_id,
        document_id="d_qwen_1",
        file_name="one.pdf",
        source_path="one.pdf",
        language="ko",
        execution_backend="qwen_gpu",
        queue_route="qwen_doc",
        model_code="qwen2.5-vl-7b",
    )
    second_item_id = create_job_item(
        job_id=job_id,
        document_id="d_qwen_2",
        file_name="two.pdf",
        source_path="two.pdf",
        language="ko",
        execution_backend="qwen_gpu",
        queue_route="qwen_doc",
        model_code="qwen2.5-vl-7b",
    )

    assert (
        mark_item_processing(first_item_id, "doc-worker-1", stage="PREPROCESSING")
        is True
    )
    assert mark_item_stage(first_item_id, "GPU_WAITING", worker_id="doc-worker-1") is True

    assert (
        mark_item_processing(second_item_id, "doc-worker-2", stage="PREPROCESSING")
        is True
    )
    assert JOB_ITEMS[first_item_id]["stage"] == "GPU_WAITING"
    assert JOB_ITEMS[second_item_id]["stage"] == "PREPROCESSING"


def test_job_parallelism_allows_multiple_items_up_to_limit() -> None:
    job_id = create_job(
        model_id="m1",
        parallelism=2,
        total_items=2,
        requested_execution_backend="auto",
        execution_backend="openai",
    )
    first_item_id = create_job_item(
        job_id=job_id,
        document_id="d_3",
        file_name="one.txt",
        source_path="one.txt",
        language="ko",
        execution_backend="openai",
        queue_route="openai",
        model_code="gpt-5.2",
    )
    second_item_id = create_job_item(
        job_id=job_id,
        document_id="d_4",
        file_name="two.txt",
        source_path="two.txt",
        language="ko",
        execution_backend="openai",
        queue_route="openai",
        model_code="gpt-5.2",
    )

    assert mark_item_processing(first_item_id, "worker-1") is True
    assert mark_item_processing(second_item_id, "worker-2") is True
    assert JOB_ITEMS[first_item_id]["status"] == "PROCESSING"
    assert JOB_ITEMS[second_item_id]["status"] == "PROCESSING"


def test_mark_item_progress_updates_document_job_and_publishes_event() -> None:
    events = []
    set_job_progress_publisher(events.append)
    try:
        job_id = create_job(
            model_id="m1",
            parallelism=1,
            total_items=1,
            requested_execution_backend="auto",
            execution_backend="openai",
        )
        item_id = create_job_item(
            job_id=job_id,
            document_id="d_progress",
            file_name="progress.pdf",
            source_path="progress.pdf",
            language="ko",
            execution_backend="openai",
            queue_route="openai",
            model_code="gpt-5.2",
        )
        DOCUMENTS["d_progress"] = {
            "documentId": "d_progress",
            "latestStatus": "QUEUED",
            "jobId": job_id,
            "jobItemId": item_id,
            "meta": {},
        }

        assert mark_item_processing(item_id, "worker-progress") is True
        assert mark_item_progress(item_id, 2, 4, worker_id="worker-progress") is True

        item = JOB_ITEMS[item_id]
        doc = DOCUMENTS["d_progress"]
        job = JOBS[job_id]
        assert item["progressPercent"] == 50
        assert item["currentPage"] == 2
        assert item["totalPages"] == 4
        assert doc["meta"]["progressPercent"] == 50
        assert doc["meta"]["processedPages"] == 2
        assert job["progressPercent"] == 50
        assert events[-1]["eventType"] == "PROGRESS"
        assert events[-1]["documentPercent"] == 50
        assert events[-1]["jobPercent"] == 50
    finally:
        set_job_progress_publisher(publish_job_progress)


def test_cleanup_old_job_events_removes_only_expired_terminal_or_orphan_events():
    now = datetime.now(timezone.utc)
    old = (now - timedelta(hours=2)).isoformat()
    recent = (now - timedelta(minutes=10)).isoformat()

    JOBS["j_done"] = {
        "jobId": "j_done",
        "status": "COMPLETED",
        "completedAt": old,
    }
    JOBS["j_active"] = {
        "jobId": "j_active",
        "status": "PROCESSING",
        "completedAt": None,
    }

    JOB_EVENTS["e_old_done"] = {
        "eventId": "e_old_done",
        "jobId": "j_done",
        "jobItemId": "ji_1",
        "eventType": "COMPLETED",
        "createdAt": old,
    }
    JOB_EVENTS["e_recent_done"] = {
        "eventId": "e_recent_done",
        "jobId": "j_done",
        "jobItemId": "ji_1",
        "eventType": "COMPLETED",
        "createdAt": recent,
    }
    JOB_EVENTS["e_old_active"] = {
        "eventId": "e_old_active",
        "jobId": "j_active",
        "jobItemId": "ji_2",
        "eventType": "STARTED",
        "createdAt": old,
    }
    JOB_EVENTS["e_old_orphan"] = {
        "eventId": "e_old_orphan",
        "jobId": "j_missing",
        "jobItemId": "ji_3",
        "eventType": "FAILED",
        "createdAt": old,
    }

    removed = cleanup_old_job_events(now=now, retention_seconds=3600)
    assert removed == 2
    assert "e_old_done" not in JOB_EVENTS
    assert "e_old_orphan" not in JOB_EVENTS
    assert "e_recent_done" in JOB_EVENTS
    assert "e_old_active" in JOB_EVENTS


def test_document_result_returns_full_text_without_preview_limit(tmp_path: Path):
    client = make_client(tmp_path)

    output_path = tmp_path / "outputs" / "long.txt"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    full_text = "A" * 2500 + "\n" + "B" * 2500
    output_path.write_text(full_text, encoding="utf-8")
    output_path.with_suffix(".meta.json").write_text('{"pageCount": 1}', encoding="utf-8")

    DOCUMENTS["d_long"] = {
        "documentId": "d_long",
        "title": "long",
        "originalFilename": "long.pdf",
        "originalFilePath": str(tmp_path / "inputs" / "long.pdf"),
        "fileType": "pdf",
        "uploadedAt": "2026-03-25T00:00:00Z",
        "updatedAt": "2026-03-25T00:00:00Z",
        "latestStatus": "COMPLETED",
        "jobId": "j_long",
        "jobItemId": "ji_long",
        "processingTimeMs": 10,
        "modelCode": "qwen2.5-vl-7b",
        "outputPath": str(output_path),
        "error": None,
    }

    result_res = client.get("/api/v1/parser/documents/d_long/result")
    assert result_res.status_code == 200

    data = result_res.json()["data"]
    assert data["txt"]["path"] == str(output_path)
    assert data["txt"]["preview"] == full_text
    assert len(data["txt"]["preview"]) > 4000
    assert data["meta"]["pageCount"] == 1

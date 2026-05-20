import importlib
import os
import sys
import types
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault("OPENAI_API_KEY", "test-key")


def make_client(tmp_path: Path, monkeypatch) -> TestClient:
    deps_module = types.ModuleType("api.dependencies")
    deps_module.config = SimpleNamespace(
        input_root=tmp_path / "inputs",
        output_root=tmp_path / "outputs",
        tmp_root=tmp_path / "tmp",
    )
    deps_module.pipeline = SimpleNamespace(
        update_vlm_model=lambda *_args, **_kwargs: None,
        process_file=lambda *_args, **_kwargs: None,
    )
    deps_module.get_auto_processor = lambda: SimpleNamespace(
        start=lambda: True,
        stop=lambda: None,
        trigger_now=lambda *_args, **_kwargs: 0,
        get_status=lambda: {"scheduler_running": False, "processing": False},
    )
    deps_module.get_config = lambda: deps_module.config
    deps_module.set_runtime_services = lambda **_kwargs: None

    original_dependencies = sys.modules.get("api.dependencies")
    try:
        sys.modules["api.dependencies"] = deps_module
        import api.routers.batch as batch_module
        import api.services.batch_jobs as batch_jobs_module

        batch_module = importlib.reload(batch_module)
        batch_jobs_module = importlib.reload(batch_jobs_module)
    finally:
        if original_dependencies is None:
            sys.modules.pop("api.dependencies", None)
        else:
            sys.modules["api.dependencies"] = original_dependencies

    monkeypatch.setattr(
        batch_jobs_module,
        "launch_worker_process",
        lambda _config: SimpleNamespace(pid=4321),
    )

    app = FastAPI()
    app.include_router(batch_module.router, prefix="/api/v1")
    return TestClient(app)


def test_batch_start_status_and_stop(tmp_path: Path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)

    input_file = tmp_path / "inputs" / "queued" / "sample.pdf"
    input_file.parent.mkdir(parents=True, exist_ok=True)
    input_file.write_bytes(b"%PDF-1.7")

    start_res = client.post("/api/v1/batch/start", json={})
    assert start_res.status_code == 200
    started = start_res.json()["data"]
    assert started["started"] is True
    assert started["workerPid"] == 4321
    assert started["totalItems"] == 1

    status_res = client.get("/api/v1/batch/status")
    assert status_res.status_code == 200
    status = status_res.json()["data"]
    assert status["status"] == "running"
    assert status["totalItems"] == 1
    assert status["workerPid"] == 4321

    stop_res = client.post("/api/v1/batch/stop")
    assert stop_res.status_code == 200
    stopped = stop_res.json()["data"]
    assert stopped["ok"] is True
    assert stopped["stopRequested"] is True

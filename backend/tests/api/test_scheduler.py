import importlib
import os
import sys
import types
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault("OPENAI_API_KEY", "test-key")


class FakeAutoProcessor:
    def __init__(self):
        self.started = False
        self.stopped = False
        self.trigger_count = 0

    def start(self):
        self.started = True
        return True

    def stop(self):
        self.stopped = True

    def trigger_now(self, language, vlm_model, parallel=1, max_retries=1):
        self.trigger_count += 1
        self.last_trigger = {
            "language": language,
            "vlmModel": vlm_model,
            "parallel": parallel,
            "maxRetries": max_retries,
        }
        return 2

    def get_status(self):
        return {
            "scheduler_running": self.started and not self.stopped,
            "processing": False,
            "within_time_window": True,
            "batch_status": "idle",
        }


def make_client(tmp_path: Path) -> tuple[TestClient, FakeAutoProcessor]:
    auto_processor = FakeAutoProcessor()

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
    deps_module.get_config = lambda: deps_module.config
    deps_module.get_auto_processor = lambda: auto_processor
    deps_module.set_runtime_services = lambda **_kwargs: None

    original_dependencies = sys.modules.get("api.dependencies")
    try:
        sys.modules["api.dependencies"] = deps_module
        import api.routers.scheduler as scheduler_module

        scheduler_module = importlib.reload(scheduler_module)
    finally:
        if original_dependencies is None:
            sys.modules.pop("api.dependencies", None)
        else:
            sys.modules["api.dependencies"] = original_dependencies

    app = FastAPI()
    app.include_router(scheduler_module.router, prefix="/api/v1")
    return TestClient(app), auto_processor


def test_scheduler_config_status_and_trigger(tmp_path: Path):
    client, auto_processor = make_client(tmp_path)

    input_file = tmp_path / "inputs" / "incoming" / "sample.pdf"
    input_file.parent.mkdir(parents=True, exist_ok=True)
    input_file.write_bytes(b"%PDF-1.7")

    config_res = client.get("/api/v1/scheduler/config")
    assert config_res.status_code == 200
    assert config_res.json()["data"]["enabled"] is False

    update_res = client.put(
        "/api/v1/scheduler/config",
        json={
            "enabled": True,
            "scheduleMode": "timed",
            "startTime": "09:00",
            "endTime": "18:00",
            "language": "한국어",
            "vlmModel": "gpt-5.2",
            "parallel": 2,
            "maxRetries": 1,
            "pollIntervalSeconds": 60,
        },
    )
    assert update_res.status_code == 200
    updated = update_res.json()["data"]
    assert updated["enabled"] is True
    assert updated["scheduleMode"] == "timed"
    assert auto_processor.started is True

    status_res = client.get("/api/v1/scheduler/status")
    assert status_res.status_code == 200
    status = status_res.json()["data"]
    assert status["unprocessedCount"] == 1

    trigger_res = client.post(
        "/api/v1/scheduler/trigger",
        json={"language": "English", "vlmModel": "gpt-5-mini", "parallel": 3, "maxRetries": 2},
    )
    assert trigger_res.status_code == 200
    triggered = trigger_res.json()["data"]
    assert triggered["queuedCount"] == 2
    assert auto_processor.trigger_count == 1

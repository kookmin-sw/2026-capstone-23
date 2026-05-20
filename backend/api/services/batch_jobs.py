from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable

from core.batch_state import load_batch_state, save_batch_state, set_stop_requested
from core.config import EXCLUDE_FILES, SUPPORTED_EXTENSIONS
from core.env import project_environ
from core.server_spec import suggest_max_parallel

from api.services.managed_files import resolve_managed_path


def _scan_input_candidates(config) -> list[dict[str, Any]]:
    input_root = config.input_root.resolve()
    if not input_root.exists():
        return []

    items: list[dict[str, Any]] = []
    for file_path in input_root.rglob("*"):
        if not file_path.is_file():
            continue
        if file_path.name.startswith(".") and file_path.name not in EXCLUDE_FILES:
            continue
        if file_path.name in EXCLUDE_FILES:
            continue
        if file_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue
        items.append({"path": str(file_path.resolve()), "is_dir": False})
    return items


def _normalize_batch_items(config, raw_paths: Iterable[str] | None) -> list[dict[str, Any]]:
    if not raw_paths:
        return _scan_input_candidates(config)

    items: list[dict[str, Any]] = []
    for raw_path in raw_paths:
        path, _, _ = resolve_managed_path(config, raw_path, scopes=("input",))
        if path.is_dir():
            items.append({"path": str(path), "is_dir": True})
            continue
        if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            raise ValueError(f"unsupported file type: {path.name}")
        items.append({"path": str(path), "is_dir": False})

    unique_items: list[dict[str, Any]] = []
    seen: set[tuple[str, bool]] = set()
    for item in items:
        key = (item["path"], item["is_dir"])
        if key in seen:
            continue
        seen.add(key)
        unique_items.append(item)
    return unique_items


def launch_worker_process(config) -> subprocess.Popen:
    project_root = Path(__file__).resolve().parents[2]
    worker_cmd = [sys.executable, "-m", "core.batch_worker"]
    log_path = config.tmp_root.resolve() / "batch_worker.log"
    config.tmp_root.mkdir(parents=True, exist_ok=True)
    log_file = open(log_path, "w", encoding="utf-8")
    try:
        process = subprocess.Popen(
            worker_cmd,
            cwd=str(project_root),
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env={**project_environ(), "PYTHONPATH": str(project_root)},
        )
    finally:
        log_file.close()
    return process


def start_background_batch(
    config,
    *,
    paths: Iterable[str] | None = None,
    language: str = "한국어",
    vlm_model: str = "openrouter/qwen3-vl-32b",
    parallel: int = 1,
    max_retries: int = 1,
) -> dict[str, Any]:
    current = load_batch_state(config)
    if current and current.get("status") == "running":
        raise RuntimeError("batch is already running")

    items = _normalize_batch_items(config, paths)
    if not items:
        return {
            "started": False,
            "workerPid": None,
            "totalItems": 0,
            "status": "idle",
            "logPath": str(config.tmp_root.resolve() / "batch_worker.log"),
        }

    state = {
        "all_items": items,
        "completed_count": 0,
        "failed_count": 0,
        "language": language,
        "vlm_model": vlm_model,
        "parallel": max(1, int(parallel)),
        "max_retries": max(0, min(3, int(max_retries))),
        "status": "running",
        "stop_requested": False,
        "source": "api",
    }
    save_batch_state(config, state)

    process = launch_worker_process(config)
    state["worker_pid"] = process.pid
    save_batch_state(config, state)

    return {
        "started": True,
        "workerPid": process.pid,
        "totalItems": len(items),
        "status": "running",
        "logPath": str((config.tmp_root.resolve() / "batch_worker.log")),
    }


def resume_background_batch(
    config,
    *,
    language: str | None = None,
    vlm_model: str | None = None,
    parallel: int | None = None,
    max_retries: int | None = None,
) -> dict[str, Any]:
    state = load_batch_state(config)
    if not state:
        raise FileNotFoundError("batch state not found")
    if state.get("status") == "running":
        raise RuntimeError("batch is already running")

    all_items = state.get("all_items", [])
    completed_count = int(state.get("completed_count", 0))
    if completed_count >= len(all_items):
        raise ValueError("batch is already completed")

    state["status"] = "running"
    state["stop_requested"] = False
    if language is not None:
        state["language"] = language
    if vlm_model is not None:
        state["vlm_model"] = vlm_model
    if parallel is not None:
        state["parallel"] = max(1, int(parallel))
    if max_retries is not None:
        state["max_retries"] = max(0, min(3, int(max_retries)))
    save_batch_state(config, state)

    process = launch_worker_process(config)
    state["worker_pid"] = process.pid
    save_batch_state(config, state)

    return {
        "started": True,
        "workerPid": process.pid,
        "totalItems": len(all_items),
        "completedItems": completed_count,
        "status": "running",
        "logPath": str((config.tmp_root.resolve() / "batch_worker.log")),
    }


def stop_background_batch(config) -> dict[str, Any]:
    state = load_batch_state(config)
    if not state:
        raise FileNotFoundError("batch state not found")

    set_stop_requested(config)
    refreshed = load_batch_state(config) or {}
    return {
        "ok": True,
        "status": refreshed.get("status", "idle"),
        "stopRequested": bool(refreshed.get("stop_requested")),
    }


def get_batch_status(config) -> dict[str, Any]:
    state = load_batch_state(config)
    if not state:
        return {
            "status": "idle",
            "totalItems": 0,
            "completedItems": 0,
            "failedItems": 0,
            "remainingItems": 0,
            "canResume": False,
            "lastOutputs": [],
            "logPath": str((config.tmp_root.resolve() / "batch_worker.log")),
        }

    total_items = len(state.get("all_items", []))
    completed_items = int(state.get("completed_count", 0))
    failed_items = int(state.get("failed_count", 0))
    return {
        "status": state.get("status", "idle"),
        "totalItems": total_items,
        "completedItems": completed_items,
        "failedItems": failed_items,
        "remainingItems": max(0, total_items - completed_items),
        "canResume": state.get("status") != "running" and completed_items < total_items,
        "stopRequested": bool(state.get("stop_requested")),
        "currentFilePath": state.get("current_file_path"),
        "currentPage": state.get("current_page"),
        "currentPageTotal": state.get("current_page_total"),
        "workerPid": state.get("worker_pid"),
        "language": state.get("language"),
        "vlmModel": state.get("vlm_model"),
        "parallel": state.get("parallel"),
        "maxRetries": state.get("max_retries"),
        "lastOutputs": state.get("last_outputs", []),
        "updatedAt": state.get("updated_at"),
        "completedAt": state.get("completed_at"),
        "logPath": str((config.tmp_root.resolve() / "batch_worker.log")),
    }


def get_server_spec_summary() -> dict[str, Any]:
    max_workers, reason, speed_prediction = suggest_max_parallel()
    return {
        "recommendedParallel": max_workers,
        "reason": reason,
        "speedPrediction": speed_prediction,
    }

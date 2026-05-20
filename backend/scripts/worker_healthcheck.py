from __future__ import annotations

import os
import socket
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse


def _tcp_check(url_value: str, default_port: int, *, label: str, errors: list[str]) -> None:
    if not url_value:
        errors.append(f"{label} URL is empty")
        return
    parsed = urlparse(url_value)
    host = parsed.hostname
    port = parsed.port or default_port
    if not host:
        errors.append(f"{label} URL has no host")
        return
    try:
        with socket.create_connection((host, port), timeout=3):
            return
    except OSError as exc:
        errors.append(f"{label} tcp check failed: {host}:{port} ({exc})")


def _tmp_writable(errors: list[str]) -> None:
    tmp_root = Path(os.environ.get("TMP_ROOT") or "/app/data/tmp")
    try:
        tmp_root.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix=".health-", dir=tmp_root, delete=True):
            return
    except OSError as exc:
        errors.append(f"TMP_ROOT is not writable: {tmp_root} ({exc})")


def _qwen_model_available(errors: list[str]) -> None:
    worker_mode = (os.environ.get("WORKER_MODE") or "").strip().lower()
    if worker_mode != "qwen_infer":
        return
    model_path = Path(os.environ.get("QWEN_VL_7B_MODEL_PATH") or "/models/Qwen2.5-VL-7B-Instruct")
    if not model_path.is_dir():
        errors.append(f"Qwen model path is not mounted or not a directory: {model_path}")
        return
    if not (model_path / "config.json").is_file():
        errors.append(f"Qwen model config is missing: {model_path / 'config.json'}")


def main() -> int:
    errors: list[str] = []
    _tcp_check(os.environ.get("REDIS_URL", "redis://redis:6379/0"), 6379, label="redis", errors=errors)
    _tcp_check(
        os.environ.get("RABBITMQ_URL", "amqp://rabbitmq:5672/%2F"),
        5672,
        label="rabbitmq",
        errors=errors,
    )
    _tmp_writable(errors)
    _qwen_model_available(errors)

    if errors:
        for error in errors:
            print(f"[worker-healthcheck] {error}", file=sys.stderr)
        return 1

    print("[worker-healthcheck] ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

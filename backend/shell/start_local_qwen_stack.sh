#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  bash shell/start_local_qwen_stack.sh [--worker-count N] [--worker-concurrency N] [--gpu-slots N] [--port N] [--dry-run]

Notes:
  --worker-count        Number of qwen_infer GPU replicas.
  --worker-concurrency  Per-replica worker concurrency. Keep this at 1 for RTX 3090.
  --gpu-slots           In-process Qwen semaphore. Keep this at 1 unless you intentionally oversubscribe.
EOF
}

WORKER_COUNT=0
WORKER_CONCURRENCY=0
GPU_SLOTS=0
PORT=0
DRY_RUN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --worker-count)
      WORKER_COUNT="${2:-}"
      shift 2
      ;;
    --worker-concurrency)
      WORKER_CONCURRENCY="${2:-}"
      shift 2
      ;;
    --gpu-slots)
      GPU_SLOTS="${2:-}"
      shift 2
      ;;
    --port)
      PORT="${2:-}"
      shift 2
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
BACKEND_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"
cd "$BACKEND_ROOT"

if [[ -x "./.venv/Scripts/python.exe" ]]; then
  PYTHON_EXE="./.venv/Scripts/python.exe"
elif [[ -x "./.venv/bin/python" ]]; then
  PYTHON_EXE="./.venv/bin/python"
elif command -v python >/dev/null 2>&1; then
  PYTHON_EXE="$(command -v python)"
else
  echo "python executable not found. expected ./.venv/Scripts/python.exe" >&2
  exit 1
fi

load_env_file() {
  local file_path="$1"
  if [[ -f "$file_path" ]]; then
    set -a
    # shellcheck source=/dev/null
    source "$file_path"
    set +a
  fi
}

load_env_file ".env"
load_env_file ".env.local"

RESOLVED_PORT="${PORT:-0}"
if [[ "$RESOLVED_PORT" == "0" ]]; then
  RESOLVED_PORT="${LOCAL_QWEN_API_PORT:-8002}"
fi

RESOLVED_WORKER_COUNT="${WORKER_COUNT:-0}"
if [[ "$RESOLVED_WORKER_COUNT" == "0" ]]; then
  RESOLVED_WORKER_COUNT="${LOCAL_QWEN_WORKER_COUNT:-2}"
fi

RESOLVED_WORKER_CONCURRENCY="${WORKER_CONCURRENCY:-0}"
if [[ "$RESOLVED_WORKER_CONCURRENCY" == "0" ]]; then
  RESOLVED_WORKER_CONCURRENCY="${LOCAL_QWEN_WORKER_CONCURRENCY:-${QWEN_INFER_WORKER_MAX_CONCURRENCY:-${QWEN_WORKER_MAX_CONCURRENCY:-${WORKER_MAX_CONCURRENCY:-1}}}}"
fi

RESOLVED_GPU_SLOTS="${GPU_SLOTS:-0}"
if [[ "$RESOLVED_GPU_SLOTS" == "0" ]]; then
  RESOLVED_GPU_SLOTS="${LOCAL_QWEN_GPU_SLOTS:-${QWEN_INFER_GPU_SLOTS:-${GPU_MAX_CONCURRENT_INFERENCE:-1}}}"
fi

LOG_DIR="data/tmp"
PID_FILE="$LOG_DIR/local-qwen-stack.json"
mkdir -p "$LOG_DIR"

if [[ -f "$PID_FILE" && "$DRY_RUN" -eq 0 ]]; then
  echo "Local Qwen stack already appears to be running: $PID_FILE" >&2
  exit 1
fi

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "[dry-run] api -> $PYTHON_EXE -u -m uvicorn api:app --host 127.0.0.1 --port $RESOLVED_PORT"
  echo "[dry-run] recovery -> $PYTHON_EXE -u -m worker.recovery"
  echo "[dry-run] qwen-doc-worker -> WORKER_MODE=qwen_doc WORKER_ID=qwen-doc-local-1 $PYTHON_EXE -u -m worker.main"
  echo "[dry-run] qwen-finalize-worker -> WORKER_MODE=qwen_finalize WORKER_ID=qwen-finalize-local-1 $PYTHON_EXE -u -m worker.main"
  worker_index=1
  while [[ "$worker_index" -le "$RESOLVED_WORKER_COUNT" ]]; do
    echo "[dry-run] qwen-infer-worker-$worker_index -> WORKER_MODE=qwen_infer QWEN_INFER_WORKER_MAX_CONCURRENCY=$RESOLVED_WORKER_CONCURRENCY WORKER_MAX_CONCURRENCY=$RESOLVED_WORKER_CONCURRENCY QWEN_INFER_GPU_SLOTS=$RESOLVED_GPU_SLOTS GPU_MAX_CONCURRENT_INFERENCE=$RESOLVED_GPU_SLOTS VLM_DEVICE=gpu $PYTHON_EXE -u -m worker.main"
    worker_index=$((worker_index + 1))
  done
  exit 0
fi

"$PYTHON_EXE" - "$BACKEND_ROOT" "$PYTHON_EXE" "$PID_FILE" "$RESOLVED_PORT" "$RESOLVED_WORKER_COUNT" "$RESOLVED_WORKER_CONCURRENCY" "$RESOLVED_GPU_SLOTS" <<'PY'
import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.request

backend_root = pathlib.Path(sys.argv[1]).resolve()
if str(backend_root) not in sys.path:
    sys.path.insert(0, str(backend_root))

from core.env import project_environ

python_exe = str(pathlib.Path(sys.argv[2]).resolve())
pid_file = pathlib.Path(sys.argv[3]).resolve()
api_port = int(sys.argv[4])
worker_count = int(sys.argv[5])
worker_concurrency = int(sys.argv[6])
gpu_slots = int(sys.argv[7])

log_dir = backend_root / "data" / "tmp"
log_dir.mkdir(parents=True, exist_ok=True)

common_env = project_environ()
common_env.update(
    {
        "ENABLE_INLINE_EXEC_WORKER": "0",
        "ENABLE_INLINE_RECOVERY_WORKER": "0",
        "PYTHONUNBUFFERED": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "VLM_DEVICE": "gpu",
    }
)


def spawn_process(name: str, argv: list[str], extra_env: dict[str, str] | None = None) -> tuple[subprocess.Popen, str]:
    env = dict(common_env)
    if extra_env:
        env.update(extra_env)

    log_path = log_dir / f"{name}.log"
    with log_path.open("wb") as log_handle:
        creationflags = 0
        if os.name == "nt":
            creationflags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            creationflags |= getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
        process = subprocess.Popen(
            argv,
            cwd=str(backend_root),
            env=env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            close_fds=False if os.name == "nt" else True,
            start_new_session=False if os.name == "nt" else True,
            creationflags=creationflags,
        )
    return process, str(log_path)


def wait_for_http(url: str, timeout_seconds: float = 20.0) -> None:
    deadline = time.time() + timeout_seconds
    last_error = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            time.sleep(0.5)
    raise RuntimeError(f"health check failed for {url}: {last_error}")


def ensure_running(process: subprocess.Popen, log_path: str, name: str) -> None:
    code = process.poll()
    if code is None:
        return
    tail = ""
    try:
        tail = pathlib.Path(log_path).read_text(encoding="utf-8", errors="replace")[-4000:]
    except Exception:
        pass
    raise RuntimeError(f"{name} exited during startup (code={code})\n{tail}")


processes: list[dict[str, object]] = []
started: list[subprocess.Popen] = []

try:
    api_proc, api_log = spawn_process(
        "qwen-api",
        [python_exe, "-u", "-m", "uvicorn", "api:app", "--host", "127.0.0.1", "--port", str(api_port)],
    )
    started.append(api_proc)
    wait_for_http(f"http://127.0.0.1:{api_port}/api/v1/health")
    ensure_running(api_proc, api_log, "qwen-api")
    processes.append({"name": "qwen-api", "pid": api_proc.pid, "logPath": api_log})

    recovery_proc, recovery_log = spawn_process(
        "qwen-recovery",
        [python_exe, "-u", "-m", "worker.recovery"],
    )
    started.append(recovery_proc)
    time.sleep(1.0)
    ensure_running(recovery_proc, recovery_log, "qwen-recovery")
    processes.append({"name": "qwen-recovery", "pid": recovery_proc.pid, "logPath": recovery_log})

    doc_proc, doc_log = spawn_process(
        "qwen-doc-worker",
        [python_exe, "-u", "-m", "worker.main"],
        extra_env={
            "WORKER_MODE": "qwen_doc",
            "WORKER_ID": "qwen-doc-local-1",
        },
    )
    started.append(doc_proc)
    time.sleep(1.0)
    ensure_running(doc_proc, doc_log, "qwen-doc-worker")
    processes.append({"name": "qwen-doc-worker", "pid": doc_proc.pid, "logPath": doc_log})

    finalize_proc, finalize_log = spawn_process(
        "qwen-finalize-worker",
        [python_exe, "-u", "-m", "worker.main"],
        extra_env={
            "WORKER_MODE": "qwen_finalize",
            "WORKER_ID": "qwen-finalize-local-1",
        },
    )
    started.append(finalize_proc)
    time.sleep(1.0)
    ensure_running(finalize_proc, finalize_log, "qwen-finalize-worker")
    processes.append({"name": "qwen-finalize-worker", "pid": finalize_proc.pid, "logPath": finalize_log})

    for index in range(1, worker_count + 1):
        worker_id = f"qwen-infer-local-{index}"
        infer_proc, infer_log = spawn_process(
            f"qwen-infer-worker-{index}",
            [python_exe, "-u", "-m", "worker.main"],
            extra_env={
                "WORKER_MODE": "qwen_infer",
                "WORKER_ID": worker_id,
                "QWEN_INFER_WORKER_MAX_CONCURRENCY": str(worker_concurrency),
                "WORKER_MAX_CONCURRENCY": str(worker_concurrency),
                "QWEN_INFER_GPU_SLOTS": str(gpu_slots),
                "GPU_MAX_CONCURRENT_INFERENCE": str(gpu_slots),
                "VLM_DEVICE": "gpu",
            },
        )
        started.append(infer_proc)
        time.sleep(1.0)
        ensure_running(infer_proc, infer_log, f"qwen-infer-worker-{index}")
        processes.append({"name": f"qwen-infer-worker-{index}", "pid": infer_proc.pid, "logPath": infer_log})

    payload = {
        "apiPort": api_port,
        "workerCount": worker_count,
        "workerConcurrency": worker_concurrency,
        "gpuSlots": gpu_slots,
        "processes": processes,
    }
    pid_file.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")

    print("Local Qwen staged stack configured.")
    print(f"API: http://127.0.0.1:{api_port}")
    print(f"Qwen infer workers: {worker_count} x concurrency {worker_concurrency}")
    print(f"GPU slots per process: {gpu_slots}")
    print(f"PID file: {pid_file}")
    for process in processes:
        print(f"- {process['name']}: pid={process['pid']}")
except Exception:
    for process in reversed(started):
        try:
            if os.name == "nt":
                subprocess.run(["taskkill.exe", "/PID", str(process.pid), "/T", "/F"], check=False, capture_output=True)
            else:
                process.terminate()
        except Exception:
            pass
    raise
PY

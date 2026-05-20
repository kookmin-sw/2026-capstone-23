#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  bash shell/start_local_all_stack_linux.sh [--conda-env NAME] [--port N] [--openai-only|--qwen-only] [--openai-worker-count N] [--openai-worker-concurrency N] [--qwen-worker-count N] [--qwen-worker-concurrency N] [--gpu-slots N] [--dry-run]

Notes:
  - Starts one shared API and one shared recovery worker.
  - Starts both OpenAI workers and Qwen staged workers by default.
  - Use --openai-only or --qwen-only to reuse this script as a mode-specific launcher.
  - Linux physical server + conda environment bootstrapper.
EOF
}

CONDA_ENV_NAME="${LOCAL_CONDA_ENV_NAME:-backend}"
STACK_KIND="${LOCAL_STACK_KIND:-all}"
START_OPENAI=1
START_QWEN=1
MODE_ARG_COUNT=0
PORT=0
OPENAI_WORKER_COUNT=0
OPENAI_WORKER_CONCURRENCY=0
QWEN_WORKER_COUNT=0
QWEN_WORKER_CONCURRENCY=0
GPU_SLOTS=0
DRY_RUN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --conda-env)
      CONDA_ENV_NAME="${2:-}"
      shift 2
      ;;
    --port)
      PORT="${2:-}"
      shift 2
      ;;
    --openai-only)
      MODE_ARG_COUNT=$((MODE_ARG_COUNT + 1))
      START_OPENAI=1
      START_QWEN=0
      if [[ "$STACK_KIND" == "all" ]]; then
        STACK_KIND="openai"
      fi
      shift
      ;;
    --qwen-only)
      MODE_ARG_COUNT=$((MODE_ARG_COUNT + 1))
      START_OPENAI=0
      START_QWEN=1
      if [[ "$STACK_KIND" == "all" ]]; then
        STACK_KIND="qwen"
      fi
      shift
      ;;
    --openai-worker-count)
      OPENAI_WORKER_COUNT="${2:-}"
      shift 2
      ;;
    --openai-worker-concurrency)
      OPENAI_WORKER_CONCURRENCY="${2:-}"
      shift 2
      ;;
    --qwen-worker-count)
      QWEN_WORKER_COUNT="${2:-}"
      shift 2
      ;;
    --qwen-worker-concurrency)
      QWEN_WORKER_CONCURRENCY="${2:-}"
      shift 2
      ;;
    --gpu-slots)
      GPU_SLOTS="${2:-}"
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

if [[ "$MODE_ARG_COUNT" -gt 1 ]]; then
  echo "use only one mode flag: --openai-only or --qwen-only" >&2
  exit 1
fi

if [[ "$START_OPENAI" -eq 0 && "$START_QWEN" -eq 0 ]]; then
  echo "at least one stack mode must be enabled" >&2
  exit 1
fi

API_PROCESS_NAME="api"
RECOVERY_PROCESS_NAME="recovery"
if [[ "$START_OPENAI" -eq 0 && "$START_QWEN" -eq 1 ]]; then
  API_PROCESS_NAME="qwen-api"
  RECOVERY_PROCESS_NAME="qwen-recovery"
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
BACKEND_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"
cd "$BACKEND_ROOT"

# shellcheck source=local_stack_lib.sh
source "$SCRIPT_DIR/local_stack_lib.sh"

load_env_file() {
  local file_path="$1"
  if [[ -f "$file_path" ]]; then
    set -a
    # shellcheck source=/dev/null
    source <(sed $'1s/^\xEF\xBB\xBF//; s/\r$//' "$file_path")
    set +a
  fi
}

load_env_file ".env"
load_env_file ".env.local"

PYTHON_EXE="$(local_stack_resolve_conda_python "$CONDA_ENV_NAME")"
if [[ -z "$PYTHON_EXE" || ! -x "$PYTHON_EXE" ]]; then
  echo "failed to resolve python executable from conda env: $CONDA_ENV_NAME" >&2
  exit 1
fi

RESOLVED_PORT="${PORT:-0}"
if [[ "$RESOLVED_PORT" == "0" ]]; then
  if [[ "$START_OPENAI" -eq 0 && "$START_QWEN" -eq 1 ]]; then
    RESOLVED_PORT="${LOCAL_QWEN_API_PORT:-8002}"
  else
    RESOLVED_PORT="${LOCAL_API_PORT:-8001}"
  fi
fi

if [[ "$START_OPENAI" -eq 1 ]]; then
  RESOLVED_OPENAI_WORKER_COUNT="${OPENAI_WORKER_COUNT:-0}"
  if [[ "$RESOLVED_OPENAI_WORKER_COUNT" == "0" ]]; then
    RESOLVED_OPENAI_WORKER_COUNT="${LOCAL_OPENAI_WORKER_COUNT:-2}"
  fi

  RESOLVED_OPENAI_WORKER_CONCURRENCY="${OPENAI_WORKER_CONCURRENCY:-0}"
  if [[ "$RESOLVED_OPENAI_WORKER_CONCURRENCY" == "0" ]]; then
    RESOLVED_OPENAI_WORKER_CONCURRENCY="${LOCAL_OPENAI_WORKER_CONCURRENCY:-${OPENAI_WORKER_MAX_CONCURRENCY:-2}}"
  fi
else
  RESOLVED_OPENAI_WORKER_COUNT=0
  RESOLVED_OPENAI_WORKER_CONCURRENCY=0
fi

if [[ "$START_QWEN" -eq 1 ]]; then
  RESOLVED_QWEN_WORKER_COUNT="${QWEN_WORKER_COUNT:-0}"
  if [[ "$RESOLVED_QWEN_WORKER_COUNT" == "0" ]]; then
    RESOLVED_QWEN_WORKER_COUNT="${LOCAL_QWEN_WORKER_COUNT:-2}"
  fi

  RESOLVED_QWEN_WORKER_CONCURRENCY="${QWEN_WORKER_CONCURRENCY:-0}"
  if [[ "$RESOLVED_QWEN_WORKER_CONCURRENCY" == "0" ]]; then
    RESOLVED_QWEN_WORKER_CONCURRENCY="${LOCAL_QWEN_WORKER_CONCURRENCY:-1}"
  fi

  RESOLVED_GPU_SLOTS="${GPU_SLOTS:-0}"
  if [[ "$RESOLVED_GPU_SLOTS" == "0" ]]; then
    RESOLVED_GPU_SLOTS="${LOCAL_QWEN_GPU_SLOTS:-1}"
  fi
else
  RESOLVED_QWEN_WORKER_COUNT=0
  RESOLVED_QWEN_WORKER_CONCURRENCY=0
  RESOLVED_GPU_SLOTS=0
fi

LOG_DIR="data/tmp"
PID_FILE="$LOG_DIR/local-${STACK_KIND}-stack.json"
if [[ "$DRY_RUN" -eq 0 ]]; then
  mkdir -p "$LOG_DIR"
fi

conflicting_pid_file_running() {
  local pid_file="$1"
  [[ -f "$pid_file" ]] || return 1
  "$PYTHON_EXE" - "$pid_file" <<'PY'
import json
import os
import pathlib
import sys

pid_file = pathlib.Path(sys.argv[1])
try:
    payload = json.loads(pid_file.read_text(encoding="utf-8"))
except Exception:
    raise SystemExit(1)

for process_info in payload.get("processes", []):
    pid = process_info.get("pid")
    if not pid:
        continue
    try:
        os.kill(int(pid), 0)
        raise SystemExit(0)
    except ProcessLookupError:
        continue
    except PermissionError:
        raise SystemExit(0)

raise SystemExit(1)
PY
}

is_pid_running() {
  local pid="$1"
  kill -0 "$pid" >/dev/null 2>&1
}

kill_pid_tree() {
  local pid="$1"
  kill "$pid" >/dev/null 2>&1 || true
}

cleanup_started() {
  local pid
  for pid in "${STARTED_PIDS[@]:-}"; do
    kill_pid_tree "$pid"
  done
}

if [[ -f "$PID_FILE" && "$DRY_RUN" -eq 0 ]]; then
  mapfile -t EXISTING_PIDS < <("$PYTHON_EXE" - "$PID_FILE" <<'PY'
import json
import pathlib
import sys

pid_file = pathlib.Path(sys.argv[1])
try:
    payload = json.loads(pid_file.read_text(encoding="utf-8"))
except Exception:
    raise SystemExit(0)

for process_info in payload.get("processes", []):
    pid = process_info.get("pid")
    if pid:
        print(pid)
PY
)

  RUNNING_PIDS=()
  for pid in "${EXISTING_PIDS[@]:-}"; do
    if [[ -n "$pid" ]] && is_pid_running "$pid"; then
      RUNNING_PIDS+=("$pid")
    fi
  done

  if (( ${#RUNNING_PIDS[@]} > 0 )); then
    echo "Local ${STACK_KIND} stack already appears to be running: $PID_FILE (pids=$(IFS=,; echo "${RUNNING_PIDS[*]}"))" >&2
    exit 1
  fi

  rm -f "$PID_FILE"
fi

if [[ "$DRY_RUN" -eq 0 ]]; then
  for stack_name in all openai qwen; do
    conflicting_pid_file="$LOG_DIR/local-${stack_name}-stack.json"
    if [[ "$conflicting_pid_file" == "$PID_FILE" ]]; then
      continue
    fi
    if conflicting_pid_file_running "$conflicting_pid_file"; then
      echo "Conflicting stack is already running: $conflicting_pid_file" >&2
      echo "Stop it first with the matching stop script, or use shell/stop_local_all_stack_linux.sh." >&2
      exit 1
    fi
  done
fi

COMMON_ENV=(
  "ENABLE_INLINE_EXEC_WORKER=0"
  "ENABLE_INLINE_RECOVERY_WORKER=0"
  "STRICT_QUEUE_BACKEND=1"
  "PYTHONUNBUFFERED=1"
  "PYTHONIOENCODING=utf-8"
  "PYTHONUTF8=1"
)

STARTED_PIDS=()
PROCESS_NAMES=()
PROCESS_PIDS=()
PROCESS_LOGS=()

spawn_process() {
  local name="$1"
  shift

  local log_path="$LOG_DIR/$name.log"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "[dry-run] $name -> $*"
    PROCESS_NAMES+=("$name")
    PROCESS_PIDS+=("")
    PROCESS_LOGS+=("$log_path")
    return 0
  fi

  : > "$log_path"
  nohup "$@" >"$log_path" 2>&1 < /dev/null &
  local pid=$!

  STARTED_PIDS+=("$pid")
  PROCESS_NAMES+=("$name")
  PROCESS_PIDS+=("$pid")
  PROCESS_LOGS+=("$log_path")
}

wait_for_http() {
  local url="$1"
  local attempts="$2"
  local sleep_seconds="$3"

  local _i
  for ((_i = 0; _i < attempts; _i += 1)); do
    if "$PYTHON_EXE" - "$url" <<'PY'
import sys
import urllib.request

url = sys.argv[1]
try:
    with urllib.request.urlopen(url, timeout=2) as response:
        raise SystemExit(0 if response.status == 200 else 1)
except Exception:
    raise SystemExit(1)
PY
    then
      return 0
    fi
    sleep "$sleep_seconds"
  done
  return 1
}

spawn_process "$API_PROCESS_NAME" env "${COMMON_ENV[@]}" "$PYTHON_EXE" -u -m uvicorn api:app --host 127.0.0.1 --port "$RESOLVED_PORT"

echo "Starting local ${STACK_KIND} stack..."
echo "Mode: ${STACK_KIND}"
echo "Conda env: ${CONDA_ENV_NAME}"
echo "Python: ${PYTHON_EXE}"
echo "API port: ${RESOLVED_PORT}"
if [[ "$START_OPENAI" -eq 1 ]]; then
  echo "OpenAI workers: ${RESOLVED_OPENAI_WORKER_COUNT} x concurrency ${RESOLVED_OPENAI_WORKER_CONCURRENCY}"
fi
if [[ "$START_QWEN" -eq 1 ]]; then
  echo "Qwen infer workers: ${RESOLVED_QWEN_WORKER_COUNT} x concurrency ${RESOLVED_QWEN_WORKER_CONCURRENCY}"
  echo "GPU slots per qwen process: ${RESOLVED_GPU_SLOTS}"
fi

if [[ "$DRY_RUN" -eq 0 ]]; then
  if ! wait_for_http "http://127.0.0.1:${RESOLVED_PORT}/api/v1/health" 40 0.5; then
    echo "API health check failed. tail of $LOG_DIR/api.log:" >&2
    tail -n 80 "$LOG_DIR/api.log" >&2 || true
    cleanup_started
    exit 1
  fi
fi

spawn_process "$RECOVERY_PROCESS_NAME" env "${COMMON_ENV[@]}" "$PYTHON_EXE" -u -m worker.recovery

if [[ "$START_OPENAI" -eq 1 ]]; then
  OPENAI_INDEX=1
  while [[ "$OPENAI_INDEX" -le "$RESOLVED_OPENAI_WORKER_COUNT" ]]; do
    spawn_process \
      "worker-openai-${OPENAI_INDEX}" \
      env \
      "${COMMON_ENV[@]}" \
      "WORKER_MODE=openai" \
      "WORKER_MAX_CONCURRENCY=${RESOLVED_OPENAI_WORKER_CONCURRENCY}" \
      "WORKER_ID=worker-openai-local-${OPENAI_INDEX}" \
      "$PYTHON_EXE" -u -m worker.main
    OPENAI_INDEX=$((OPENAI_INDEX + 1))
  done
fi

if [[ "$START_QWEN" -eq 1 ]]; then
  spawn_process \
    "qwen-doc-worker" \
    env \
    "${COMMON_ENV[@]}" \
    "WORKER_MODE=qwen_doc" \
    "WORKER_ID=qwen-doc-local-1" \
    "$PYTHON_EXE" -u -m worker.main

  spawn_process \
    "qwen-finalize-worker" \
    env \
    "${COMMON_ENV[@]}" \
    "WORKER_MODE=qwen_finalize" \
    "WORKER_ID=qwen-finalize-local-1" \
    "$PYTHON_EXE" -u -m worker.main

  QWEN_INDEX=1
  while [[ "$QWEN_INDEX" -le "$RESOLVED_QWEN_WORKER_COUNT" ]]; do
    spawn_process \
      "qwen-infer-worker-${QWEN_INDEX}" \
      env \
      "${COMMON_ENV[@]}" \
      "WORKER_MODE=qwen_infer" \
      "WORKER_MAX_CONCURRENCY=${RESOLVED_QWEN_WORKER_CONCURRENCY}" \
      "QWEN_INFER_WORKER_MAX_CONCURRENCY=${RESOLVED_QWEN_WORKER_CONCURRENCY}" \
      "GPU_MAX_CONCURRENT_INFERENCE=${RESOLVED_GPU_SLOTS}" \
      "QWEN_INFER_GPU_SLOTS=${RESOLVED_GPU_SLOTS}" \
      "VLM_DEVICE=gpu" \
      "WORKER_ID=qwen-infer-local-${QWEN_INDEX}" \
      "$PYTHON_EXE" -u -m worker.main
    QWEN_INDEX=$((QWEN_INDEX + 1))
  done
fi

if [[ "$DRY_RUN" -eq 0 ]]; then
  sleep 1
  for index in "${!PROCESS_PIDS[@]}"; do
    pid="${PROCESS_PIDS[$index]}"
    if [[ -z "$pid" ]] || ! is_pid_running "$pid"; then
      echo "process startup failed: ${PROCESS_NAMES[$index]}" >&2
      tail -n 80 "${PROCESS_LOGS[$index]}" >&2 || true
      cleanup_started
      exit 1
    fi
  done
fi

if [[ "$DRY_RUN" -eq 0 ]]; then
  PROCESS_STATE_FILE="$LOG_DIR/.local-${STACK_KIND}-stack-processes.tsv"
  : > "$PROCESS_STATE_FILE"
  for index in "${!PROCESS_NAMES[@]}"; do
    clean_name="$(printf '%s' "${PROCESS_NAMES[$index]}" | tr -d '\r\n')"
    clean_pid="$(printf '%s' "${PROCESS_PIDS[$index]}" | tr -d '\r\n[:space:]')"
    clean_log="$(printf '%s' "${PROCESS_LOGS[$index]}" | tr -d '\r\n')"
    printf '%s\t%s\t%s\n' "$clean_name" "$clean_pid" "$clean_log" >>"$PROCESS_STATE_FILE"
  done

  "$PYTHON_EXE" - "$PID_FILE" "$STACK_KIND" "$RESOLVED_PORT" "$RESOLVED_OPENAI_WORKER_COUNT" "$RESOLVED_OPENAI_WORKER_CONCURRENCY" "$RESOLVED_QWEN_WORKER_COUNT" "$RESOLVED_QWEN_WORKER_CONCURRENCY" "$RESOLVED_GPU_SLOTS" "$PROCESS_STATE_FILE" "$CONDA_ENV_NAME" "$PYTHON_EXE" <<'PY'
import json
import pathlib
import sys

pid_file = pathlib.Path(sys.argv[1])
stack_kind = sys.argv[2]
api_port = int(sys.argv[3])
openai_worker_count = int(sys.argv[4])
openai_worker_concurrency = int(sys.argv[5])
qwen_worker_count = int(sys.argv[6])
qwen_worker_concurrency = int(sys.argv[7])
gpu_slots = int(sys.argv[8])
process_state_file = pathlib.Path(sys.argv[9])
conda_env_name = sys.argv[10]
python_exe = sys.argv[11]

lines = []
if process_state_file.exists():
    lines = [line.rstrip("\n") for line in process_state_file.read_text(encoding="utf-8").splitlines() if line.strip()]
processes = []
for line in lines:
    name, pid, log_path = line.split("\t", 2)
    processes.append(
        {
            "name": name,
            "pid": int(pid) if pid else None,
            "logPath": log_path,
        }
    )

payload = {
    "stackKind": stack_kind,
    "apiPort": api_port,
    "openaiWorkerCount": openai_worker_count,
    "openaiWorkerConcurrency": openai_worker_concurrency,
    "qwenWorkerCount": qwen_worker_count,
    "qwenWorkerConcurrency": qwen_worker_concurrency,
    "gpuSlots": gpu_slots,
    "condaEnvName": conda_env_name,
    "pythonExe": python_exe,
    "processes": processes,
}
if stack_kind == "openai":
    payload["workerCount"] = openai_worker_count
    payload["workerConcurrency"] = openai_worker_concurrency
elif stack_kind == "qwen":
    payload["workerCount"] = qwen_worker_count
    payload["workerConcurrency"] = qwen_worker_concurrency

pid_file.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")
PY
  rm -f "$PROCESS_STATE_FILE"
fi

echo "Local ${STACK_KIND} stack configured."
echo "Mode: ${STACK_KIND}"
echo "Conda env: ${CONDA_ENV_NAME}"
echo "Python: ${PYTHON_EXE}"
echo "API: http://127.0.0.1:${RESOLVED_PORT}"
if [[ "$START_OPENAI" -eq 1 ]]; then
  echo "OpenAI workers: ${RESOLVED_OPENAI_WORKER_COUNT} x concurrency ${RESOLVED_OPENAI_WORKER_CONCURRENCY}"
fi
if [[ "$START_QWEN" -eq 1 ]]; then
  echo "Qwen infer workers: ${RESOLVED_QWEN_WORKER_COUNT} x concurrency ${RESOLVED_QWEN_WORKER_CONCURRENCY}"
  echo "GPU slots per qwen process: ${RESOLVED_GPU_SLOTS}"
fi
if [[ "$DRY_RUN" -eq 0 ]]; then
  echo "PID file: ${BACKEND_ROOT}/${PID_FILE}"
  for index in "${!PROCESS_NAMES[@]}"; do
    echo "- ${PROCESS_NAMES[$index]}: pid=${PROCESS_PIDS[$index]}"
  done
fi

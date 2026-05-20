#!/usr/bin/env bash

local_stack_resolve_conda_python() {
  local env_name="${1:-backend}"

  if ! command -v conda >/dev/null 2>&1; then
    echo "conda command not found" >&2
    return 1
  fi

  if [[ "${CONDA_DEFAULT_ENV:-}" == "$env_name" ]] && command -v python >/dev/null 2>&1; then
    command -v python
    return 0
  fi

  conda run -n "$env_name" python -c 'import sys; print(sys.executable)' 2>/dev/null | tail -n 1 | tr -d '\r'
}

local_stack_stop_pid_files() {
  local python_exe="$1"
  local backend_root="$2"
  shift 2

  "$python_exe" - "$backend_root" "$@" <<'PY'
import json
import os
import pathlib
import signal
import sys
import time

backend_root = pathlib.Path(sys.argv[1]).resolve()
pid_files = [(backend_root / value).resolve() for value in sys.argv[2:]]


def is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def stop_pid(pid: int, name: str) -> None:
    if not is_running(pid):
        print(f"Already stopped or missing: {name} (pid={pid})")
        return

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        print(f"Already stopped or missing: {name} (pid={pid})")
        return

    deadline = time.time() + 10.0
    while time.time() < deadline:
        if not is_running(pid):
            print(f"Stopped {name} (pid={pid})")
            return
        time.sleep(0.2)

    try:
        os.kill(pid, signal.SIGKILL)
        print(f"Killed {name} (pid={pid})")
    except ProcessLookupError:
        print(f"Stopped {name} (pid={pid})")


removed_any = False

for pid_file in pid_files:
    if not pid_file.exists():
        continue

    try:
        payload = json.loads(pid_file.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"Failed to read pid file: {pid_file} ({exc})", file=sys.stderr)
        continue

    for process_info in payload.get("processes", []):
        pid = process_info.get("pid")
        name = str(process_info.get("name") or "unknown")
        if not pid:
            continue
        stop_pid(int(pid), name)

    pid_file.unlink(missing_ok=True)
    removed_any = True
    print(f"Removed pid file: {pid_file}")

if not removed_any:
    print("No local stack pid files found.")
PY
}

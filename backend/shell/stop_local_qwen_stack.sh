#!/usr/bin/env bash
set -euo pipefail

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

"$PYTHON_EXE" - <<'PY'
import json
import os
import pathlib
import signal
import subprocess

backend_root = pathlib.Path.cwd().resolve()
pid_file = backend_root / "data" / "tmp" / "local-qwen-stack.json"


def stop_pid(pid: int, name: str) -> None:
    if os.name == "nt":
        completed = subprocess.run(
            ["taskkill.exe", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode == 0:
            print(f"Stopped {name} (pid={pid})")
        else:
            print(f"Already stopped or missing: {name} (pid={pid})")
        return

    try:
        os.kill(pid, signal.SIGTERM)
        print(f"Stopped {name} (pid={pid})")
    except ProcessLookupError:
        print(f"Already stopped or missing: {name} (pid={pid})")


if not pid_file.exists():
    print(f"No local qwen stack pid file found: {pid_file}")
    raise SystemExit(0)

payload = json.loads(pid_file.read_text(encoding="utf-8"))
for process_info in payload.get("processes", []):
    pid = process_info.get("pid")
    if not pid:
        continue
    stop_pid(int(pid), str(process_info.get("name", "unknown")))

pid_file.unlink(missing_ok=True)
print(f"Removed pid file: {pid_file}")
PY

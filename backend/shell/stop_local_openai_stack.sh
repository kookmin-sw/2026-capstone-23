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
import shutil

backend_root = pathlib.Path.cwd().resolve()
pid_file = backend_root / "data" / "tmp" / "local-openai-stack.json"


def _stop_pid(pid: int, name: str) -> None:
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
    else:
        try:
            os.kill(pid, signal.SIGTERM)
            print(f"Stopped {name} (pid={pid})")
        except ProcessLookupError:
            print(f"Already stopped or missing: {name} (pid={pid})")


def _fallback_stop_orphan_processes() -> None:
    if os.name != "nt":
        return

    powershell_exe = shutil.which("powershell.exe") or r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
    if not pathlib.Path(powershell_exe).exists():
        print(f"No local stack pid file found: {pid_file}")
        return

    script = rf"""
$backend = '{str(backend_root).replace("'", "''")}'
Get-CimInstance Win32_Process |
  Where-Object {{
    $_.Name -like 'python*' -and
    $_.CommandLine -like ('*' + $backend + '*') -and
    (
      $_.CommandLine -like '*-m uvicorn api:app*' -or
      $_.CommandLine -like '*-m worker.recovery*' -or
      $_.CommandLine -like '*-m worker.main*'
    )
  }} |
  Select-Object -ExpandProperty ProcessId
"""
    completed = subprocess.run(
        [powershell_exe, "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        check=False,
    )
    pids = []
    for line in completed.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            pids.append(int(line))
        except ValueError:
            continue

    if not pids:
        print(f"No local stack pid file found: {pid_file}")
        return

    print(f"No local stack pid file found. Stopping orphan local stack processes: {', '.join(str(pid) for pid in pids)}")
    for pid in pids:
        _stop_pid(pid, "local-stack-orphan")


def _stop_started_infra(payload: dict) -> None:
    infra = payload.get("infra")
    if not isinstance(infra, dict):
        return

    services = [str(service) for service in infra.get("startedServices", []) if str(service).strip()]
    if not services:
        return

    compose_file = pathlib.Path(str(infra.get("composeFile") or (backend_root / "docker-compose.yml"))).resolve()
    compose_project = str(infra.get("composeProject") or "cap-be-local-openai")
    docker_exe = shutil.which("docker") or shutil.which("docker.exe")
    if not docker_exe:
        print(f"docker executable not found. could not stop infra services: {', '.join(services)}")
        return

    completed = subprocess.run(
        [docker_exe, "compose", "--project-name", compose_project, "-f", str(compose_file), "stop", *services],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(backend_root),
    )
    if completed.returncode == 0:
        print(f"Stopped infra services: {', '.join(services)}")
        return

    details = "\n".join(part for part in (completed.stdout.strip(), completed.stderr.strip()) if part)
    print(f"Failed to stop infra services ({', '.join(services)}): {details or 'unknown error'}")

if not pid_file.exists():
    _fallback_stop_orphan_processes()
    raise SystemExit(0)

payload = json.loads(pid_file.read_text(encoding="utf-8"))

for process_info in payload.get("processes", []):
    pid = process_info.get("pid")
    name = process_info.get("name", "unknown")
    if not pid:
        continue
    pid = int(pid)
    _stop_pid(pid, name)

_stop_started_infra(payload)
pid_file.unlink(missing_ok=True)
print(f"Removed pid file: {pid_file}")
PY

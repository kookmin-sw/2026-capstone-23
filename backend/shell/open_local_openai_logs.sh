#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  bash shell/open_local_openai_logs.sh [--split] [--tail-lines N] [--dry-run]

Options:
  --split         Open one extra terminal per log file.
  --tail-lines N  Number of lines to show initially. Default: 50
  --dry-run       Print what would be opened without launching terminals.
EOF
}

SPLIT=0
TAIL_LINES=50
DRY_RUN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --split)
      SPLIT=1
      shift
      ;;
    --tail-lines)
      TAIL_LINES="${2:-}"
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

"$PYTHON_EXE" - "$SPLIT" "$TAIL_LINES" "$DRY_RUN" <<'PY'
import os
import json
import pathlib
import shlex
import subprocess
import sys


def resolve_git_bash() -> pathlib.Path:
    candidates = [
        pathlib.Path(r"C:\Program Files\Git\bin\bash.exe"),
        pathlib.Path(r"C:\Program Files\Git\usr\bin\bash.exe"),
        pathlib.Path(r"C:\Program Files\Git\git-bash.exe"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise SystemExit("Git Bash executable not found under C:\\Program Files\\Git")


def to_posix_path(path: pathlib.Path) -> str:
    drive = path.drive.rstrip(":").lower()
    remainder = path.as_posix().split(":", 1)[-1]
    if remainder.startswith("/"):
        remainder = remainder[1:]
    return f"/{drive}/{remainder}"


def window_title(title: str) -> str:
    escaped = title.replace("\\", "\\\\").replace("'", "'\"'\"'")
    return f"printf '\\033]0;{escaped}\\007'; "


def launch_window(
    *,
    bash_exe: pathlib.Path,
    backend_root: pathlib.Path,
    title: str,
    shell_command: str,
    env: dict[str, str],
    dry_run: bool,
) -> None:
    command = [
        str(bash_exe),
        "-lc",
        shell_command,
    ]
    if dry_run:
        print(f"[dry-run] {title} -> {' '.join(shlex.quote(part) for part in command)}")
        return

    creationflags = 0
    if os.name == "nt":
        creationflags |= getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
        creationflags |= getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)

    subprocess.Popen(
        command,
        cwd=str(backend_root),
        env=env,
        creationflags=creationflags,
        close_fds=False if os.name == "nt" else True,
        start_new_session=False if os.name == "nt" else True,
    )


backend_root = pathlib.Path.cwd().resolve()
if str(backend_root) not in sys.path:
    sys.path.insert(0, str(backend_root))

from core.env import project_environ

log_dir = backend_root / "data" / "tmp"
log_dir.mkdir(parents=True, exist_ok=True)
pid_file = log_dir / "local-openai-stack.json"

split = bool(int(sys.argv[1]))
tail_lines = int(sys.argv[2])
dry_run = bool(int(sys.argv[3]))
bash_exe = resolve_git_bash()
base_env = project_environ()
backend_root_posix = to_posix_path(backend_root)

log_paths: list[pathlib.Path] = []
if pid_file.exists():
    try:
        payload = json.loads(pid_file.read_text(encoding="utf-8"))
        for process_info in payload.get("processes", []):
            log_path = process_info.get("logPath")
            if not log_path:
                continue
            resolved = pathlib.Path(log_path)
            if not resolved.is_absolute():
                resolved = (backend_root / resolved).resolve()
            log_paths.append(resolved)
    except Exception:
        log_paths = []

if not log_paths:
    log_paths = [
        log_dir / "api.log",
        log_dir / "recovery.log",
    ]
    worker_logs = sorted(log_dir.glob("worker-openai-*.log"))
    if not worker_logs:
        worker_logs = [log_dir / "worker-openai-1.log"]
    log_paths.extend(worker_logs)

deduped_paths: list[pathlib.Path] = []
seen_paths: set[pathlib.Path] = set()
for path in log_paths:
    resolved = path.resolve()
    if resolved in seen_paths:
        continue
    seen_paths.add(resolved)
    deduped_paths.append(resolved)
log_paths = deduped_paths

for path in log_paths:
    path.touch(exist_ok=True)

if split:
    for path in log_paths:
        relative_path = path.relative_to(backend_root).as_posix()
        title = f"cap_be {path.stem}"
        shell_command = (
            window_title(title)
            + f"cd {shlex.quote(backend_root_posix)} && "
            + f"echo '[log] {relative_path}' && "
            + f"exec tail -n {tail_lines} -F {shlex.quote(relative_path)}"
        )
        launch_window(
            bash_exe=bash_exe,
            backend_root=backend_root,
            title=title,
            shell_command=shell_command,
            env=base_env,
            dry_run=dry_run,
        )
    print(f"Opened {len(log_paths)} log terminal(s).")
else:
    relative_paths = [path.relative_to(backend_root).as_posix() for path in log_paths]
    title = "cap_be local logs"
    shell_command = (
        window_title(title)
        + f"cd {shlex.quote(backend_root_posix)} && "
        + "echo '[log] following api/recovery/worker logs' && "
        + "exec tail "
        + f"-n {tail_lines} -F "
        + " ".join(shlex.quote(path) for path in relative_paths)
    )
    launch_window(
        bash_exe=bash_exe,
        backend_root=backend_root,
        title=title,
        shell_command=shell_command,
        env=base_env,
        dry_run=dry_run,
    )
    print("Opened 1 combined log terminal.")
PY

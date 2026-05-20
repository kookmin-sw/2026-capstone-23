#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  bash shell/start_local_openai_stack.sh [--worker-count N] [--worker-concurrency N] [--port N] [--dry-run]

Notes:
  - Starts local API/recovery/OpenAI workers on the host.
  - Starts Redis and RabbitMQ through Docker Compose when they are not already reachable.
EOF
}

WORKER_COUNT=0
WORKER_CONCURRENCY=0
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
  RESOLVED_PORT="${LOCAL_API_PORT:-8001}"
fi

RESOLVED_WORKER_COUNT="${WORKER_COUNT:-0}"
if [[ "$RESOLVED_WORKER_COUNT" == "0" ]]; then
  RESOLVED_WORKER_COUNT="${LOCAL_OPENAI_WORKER_COUNT:-2}"
fi

RESOLVED_WORKER_CONCURRENCY="${WORKER_CONCURRENCY:-0}"
if [[ "$RESOLVED_WORKER_CONCURRENCY" == "0" ]]; then
  RESOLVED_WORKER_CONCURRENCY="${LOCAL_OPENAI_WORKER_CONCURRENCY:-${OPENAI_WORKER_MAX_CONCURRENCY:-2}}"
fi

LOG_DIR="data/tmp"
PID_FILE="$LOG_DIR/local-openai-stack.json"
mkdir -p "$LOG_DIR"

"$PYTHON_EXE" - "$PYTHON_EXE" "$RESOLVED_PORT" "$RESOLVED_WORKER_COUNT" "$RESOLVED_WORKER_CONCURRENCY" "$DRY_RUN" <<'PY'
from __future__ import annotations

import json
import os
import pathlib
import shlex
import shutil
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request

import pika


backend_root = pathlib.Path.cwd().resolve()
if str(backend_root) not in sys.path:
    sys.path.insert(0, str(backend_root))

from core.env import project_environ

python_exe = str(pathlib.Path(sys.argv[1]).resolve())
pid_file = (backend_root / "data" / "tmp" / "local-openai-stack.json").resolve()
api_port = int(sys.argv[2])
worker_count = int(sys.argv[3])
worker_concurrency = int(sys.argv[4])
dry_run = bool(int(sys.argv[5]))

log_dir = backend_root / "data" / "tmp"
log_dir.mkdir(parents=True, exist_ok=True)


def is_process_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        completed = subprocess.run(
            ["tasklist.exe", "/FI", f"PID eq {pid}"],
            capture_output=True,
            text=True,
            check=False,
        )
        return str(pid) in completed.stdout
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def ensure_pid_file_not_active() -> None:
    if not pid_file.exists() or dry_run:
        return

    try:
        payload = json.loads(pid_file.read_text(encoding="utf-8"))
    except Exception:
        pid_file.unlink(missing_ok=True)
        return

    running = []
    for process_info in payload.get("processes", []):
        pid = process_info.get("pid")
        if isinstance(pid, int) and is_process_running(pid):
            running.append(f"{process_info.get('name', 'unknown')}={pid}")

    if running:
        raise SystemExit(
            "Local OpenAI stack already appears to be running: "
            f"{pid_file} ({', '.join(running)})"
        )

    pid_file.unlink(missing_ok=True)


def normalize_local_url(
    raw_url: str,
    *,
    default_scheme: str,
    default_port: int,
    default_path: str,
    docker_aliases: set[str],
) -> str:
    local_hostnames = {"localhost", "127.0.0.1", "::1", *docker_aliases}
    stripped = raw_url.strip()
    if not stripped:
        scheme = default_scheme
        hostname = "127.0.0.1"
        port = default_port
        username = None
        password = None
        path = default_path
        query = ""
        fragment = ""
    else:
        parsed = urllib.parse.urlsplit(stripped)
        scheme = parsed.scheme or default_scheme
        hostname = parsed.hostname or "127.0.0.1"
        if hostname.lower() in local_hostnames:
            hostname = "127.0.0.1"
        port = parsed.port or default_port
        username = urllib.parse.unquote(parsed.username) if parsed.username else None
        password = urllib.parse.unquote(parsed.password) if parsed.password else None
        path = parsed.path or default_path
        query = parsed.query
        fragment = parsed.fragment

    auth = ""
    if username is not None:
        auth = urllib.parse.quote(username, safe="")
        if password is not None:
            auth += ":" + urllib.parse.quote(password, safe="")
        auth += "@"

    if ":" in hostname and not hostname.startswith("["):
        host_part = f"[{hostname}]"
    else:
        host_part = hostname

    netloc = f"{auth}{host_part}:{port}"
    return urllib.parse.urlunsplit((scheme, netloc, path, query, fragment))


def url_host_port(raw_url: str, default_port: int) -> tuple[str, int]:
    parsed = urllib.parse.urlsplit(raw_url)
    return parsed.hostname or "127.0.0.1", parsed.port or default_port


def is_tcp_reachable(host: str, port: int, timeout_seconds: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout_seconds):
            return True
    except OSError:
        return False


def is_redis_ready(redis_url: str) -> bool:
    host, port = url_host_port(redis_url, 6379)
    try:
        with socket.create_connection((host, port), timeout=1.5) as sock:
            sock.sendall(b"*1\r\n$4\r\nPING\r\n")
            response = sock.recv(64)
        return response.startswith(b"+PONG")
    except OSError:
        return False


def is_rabbitmq_ready(rabbitmq_url: str) -> bool:
    try:
        connection = pika.BlockingConnection(pika.URLParameters(rabbitmq_url))
    except Exception:
        return False
    try:
        return connection.is_open
    finally:
        try:
            connection.close()
        except Exception:
            pass


def format_command(argv: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in argv)


base_env = project_environ()

redis_port_default = int(base_env.get("REDIS_PORT", "6379"))
rabbitmq_port_default = int(base_env.get("RABBITMQ_PORT", "5672"))
rabbitmq_management_port = int(base_env.get("RABBITMQ_MANAGEMENT_PORT", "15672"))

redis_url = normalize_local_url(
    base_env.get("REDIS_URL", ""),
    default_scheme="redis",
    default_port=redis_port_default,
    default_path="/0",
    docker_aliases={"redis"},
)
rabbitmq_url = normalize_local_url(
    base_env.get("RABBITMQ_URL", ""),
    default_scheme="amqp",
    default_port=rabbitmq_port_default,
    default_path="/%2F",
    docker_aliases={"rabbitmq"},
)

common_env = dict(base_env)
common_env.update(
    {
        "DEFAULT_AUTO_EXECUTION_BACKEND": "openai",
        "ENABLE_INLINE_EXEC_WORKER": "0",
        "ENABLE_INLINE_RECOVERY_WORKER": "0",
        "PYTHONUNBUFFERED": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "QUEUE_BACKEND": "rabbitmq",
        "STATUS_CACHE_BACKEND": "redis",
        "REDIS_URL": redis_url,
        "RABBITMQ_URL": rabbitmq_url,
    }
)

compose_file = backend_root / "docker-compose.yml"
compose_project = base_env.get("LOCAL_OPENAI_COMPOSE_PROJECT", "cap-be-local-openai")
docker_exe = shutil.which("docker") or shutil.which("docker.exe")


def compose_cmd(*args: str) -> list[str]:
    if not docker_exe:
        raise RuntimeError("docker executable not found in PATH")
    return [docker_exe, "compose", "--project-name", compose_project, "-f", str(compose_file), *args]


def ensure_docker_compose_available() -> None:
    if not docker_exe:
        raise RuntimeError("docker executable not found. start Docker Desktop or install Docker first.")
    compose_check = subprocess.run(
        [docker_exe, "compose", "version"],
        capture_output=True,
        text=True,
        check=False,
    )
    if compose_check.returncode != 0:
        details = (compose_check.stderr or compose_check.stdout or "").strip()
        raise RuntimeError(f"docker compose is not available: {details}")
    daemon_check = subprocess.run(
        [docker_exe, "info", "--format", "{{.ServerVersion}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if daemon_check.returncode != 0:
        details = (daemon_check.stderr or daemon_check.stdout or "").strip()
        raise RuntimeError(
            "docker daemon is not running. start Docker Desktop first or bring up Redis/RabbitMQ manually.\n"
            f"{details}"
        )


def run_command(cmd: list[str], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        cmd,
        cwd=str(backend_root),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        details = "\n".join(part for part in (completed.stdout.strip(), completed.stderr.strip()) if part)
        raise RuntimeError(f"command failed: {format_command(cmd)}\n{details}")
    return completed


def resolve_compose_container_id(service_name: str) -> str:
    completed = run_command(compose_cmd("ps", "-q", service_name), env=common_env)
    return completed.stdout.strip().splitlines()[-1] if completed.stdout.strip() else ""


def inspect_container_status(container_id: str) -> str:
    completed = run_command(
        [
            docker_exe,
            "inspect",
            "-f",
            "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}",
            container_id,
        ]
    )
    return completed.stdout.strip()


def wait_for_compose_service(service_name: str, *, timeout_seconds: float = 90.0) -> None:
    deadline = time.time() + timeout_seconds
    last_status = ""
    while time.time() < deadline:
        container_id = resolve_compose_container_id(service_name)
        if container_id:
            status = inspect_container_status(container_id)
            if status:
                last_status = status
            if status in {"healthy", "running"}:
                return
        time.sleep(1.0)
    raise RuntimeError(f"{service_name} did not become healthy (last_status={last_status or 'unknown'})")


def wait_until_ready(label: str, predicate, *, timeout_seconds: float = 30.0) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.5)
    raise RuntimeError(f"{label} did not become reachable within {timeout_seconds:.0f}s")


def stop_compose_services(services: list[str]) -> None:
    if not services or not docker_exe:
        return
    subprocess.run(
        compose_cmd("stop", *services),
        cwd=str(backend_root),
        env=common_env,
        capture_output=True,
        text=True,
        check=False,
    )


def wait_for_http(urls: list[str], timeout_seconds: float = 20.0) -> str:
    deadline = time.time() + timeout_seconds
    last_error = None
    while time.time() < deadline:
        for url in urls:
            try:
                with urllib.request.urlopen(url, timeout=2) as response:
                    if response.status == 200:
                        return url
            except Exception as exc:  # noqa: BLE001
                last_error = exc
            time.sleep(0.5)
    raise RuntimeError(f"health check failed for {', '.join(urls)}: {last_error}")


def tail_text(log_path: str) -> str:
    try:
        return pathlib.Path(log_path).read_text(encoding="utf-8", errors="replace")[-4000:]
    except Exception:
        return ""


def spawn_process(
    name: str,
    argv: list[str],
    *,
    extra_env: dict[str, str] | None = None,
) -> tuple[subprocess.Popen, str]:
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
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            close_fds=True,
            start_new_session=False if os.name == "nt" else True,
            creationflags=creationflags,
        )
    return process, str(log_path)


def ensure_running(process: subprocess.Popen, log_path: str, name: str) -> None:
    code = process.poll()
    if code is None:
        return
    raise RuntimeError(f"{name} exited during startup (code={code})\n{tail_text(log_path)}")


ensure_pid_file_not_active()

redis_ready = is_redis_ready(redis_url)
rabbitmq_ready = is_rabbitmq_ready(rabbitmq_url)
missing_infra = []
if not redis_ready:
    missing_infra.append("redis")
if not rabbitmq_ready:
    missing_infra.append("rabbitmq")

if dry_run:
    print(f"[dry-run] REDIS_URL={redis_url}")
    print(f"[dry-run] RABBITMQ_URL={rabbitmq_url}")
    if missing_infra:
        print(f"[dry-run] infra missing: {', '.join(missing_infra)}")
        print(f"[dry-run] {format_command(compose_cmd('up', '-d', *missing_infra))}")
    else:
        print("[dry-run] infra already reachable: redis, rabbitmq")
    print(f"[dry-run] api -> {format_command([python_exe, '-u', '-m', 'uvicorn', 'api:app', '--host', '127.0.0.1', '--port', str(api_port)])}")
    print(f"[dry-run] recovery -> {format_command([python_exe, '-u', '-m', 'worker.recovery'])}")
    for index in range(1, worker_count + 1):
        worker_id = f"worker-openai-local-{index}"
        print(
            "[dry-run] "
            f"worker-openai-{index} -> WORKER_MODE=openai WORKER_MAX_CONCURRENCY={worker_concurrency} "
            f"WORKER_ID={worker_id} {format_command([python_exe, '-u', '-m', 'worker.main'])}"
        )
    raise SystemExit(0)

started_infra: list[str] = []
started_processes: list[subprocess.Popen] = []
processes: list[dict[str, object]] = []

try:
    if missing_infra:
        ensure_docker_compose_available()
        run_command(compose_cmd("up", "-d", *missing_infra), env=common_env)
        for service_name in missing_infra:
            wait_for_compose_service(service_name)
            started_infra.append(service_name)
        wait_until_ready("redis", lambda: is_redis_ready(redis_url), timeout_seconds=30.0)
        wait_until_ready("rabbitmq", lambda: is_rabbitmq_ready(rabbitmq_url), timeout_seconds=30.0)

    api_process, api_log = spawn_process(
        "api",
        [python_exe, "-u", "-m", "uvicorn", "api:app", "--host", "127.0.0.1", "--port", str(api_port)],
    )
    started_processes.append(api_process)
    health_url = wait_for_http(
        [
            f"http://127.0.0.1:{api_port}/v1/health",
            f"http://127.0.0.1:{api_port}/api/v1/health",
        ]
    )
    ensure_running(api_process, api_log, "api")
    processes.append({"name": "api", "pid": api_process.pid, "logPath": api_log})

    recovery_process, recovery_log = spawn_process(
        "recovery",
        [python_exe, "-u", "-m", "worker.recovery"],
    )
    started_processes.append(recovery_process)
    time.sleep(1.0)
    ensure_running(recovery_process, recovery_log, "recovery")
    processes.append({"name": "recovery", "pid": recovery_process.pid, "logPath": recovery_log})

    for index in range(1, worker_count + 1):
        logical_name = f"worker-openai-{index}"
        worker_id = f"worker-openai-local-{index}"
        worker_process, worker_log = spawn_process(
            logical_name,
            [python_exe, "-u", "-m", "worker.main"],
            extra_env={
                "WORKER_MODE": "openai",
                "WORKER_MAX_CONCURRENCY": str(worker_concurrency),
                "WORKER_ID": worker_id,
            },
        )
        started_processes.append(worker_process)
        time.sleep(1.0)
        ensure_running(worker_process, worker_log, logical_name)
        processes.append({"name": logical_name, "pid": worker_process.pid, "logPath": worker_log})

    payload = {
        "apiPort": api_port,
        "workerCount": worker_count,
        "workerConcurrency": worker_concurrency,
        "infra": {
            "composeProject": compose_project,
            "composeFile": str(compose_file),
            "startedServices": started_infra,
            "redisUrl": redis_url,
            "redisPort": url_host_port(redis_url, redis_port_default)[1],
            "rabbitmqUrl": rabbitmq_url,
            "rabbitmqPort": url_host_port(rabbitmq_url, rabbitmq_port_default)[1],
            "rabbitmqManagementPort": rabbitmq_management_port,
        },
        "processes": processes,
    }
    pid_file.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")

    print("Local OpenAI stack configured.")
    print(f"API: http://127.0.0.1:{api_port}")
    print(f"Health: {health_url}")
    print(f"Redis: {redis_url}")
    print(f"RabbitMQ: {rabbitmq_url}")
    print(f"Workers: {worker_count} x concurrency {worker_concurrency}")
    print(f"PID file: {pid_file}")
    for process_info in processes:
        print(f"- {process_info['name']}: pid={process_info['pid']}")
    if started_infra:
        print(f"Started infra services: {', '.join(started_infra)}")
    else:
        print("Infra services already reachable; Docker Compose start skipped.")
except Exception:
    for process in reversed(started_processes):
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                    check=False,
                    capture_output=True,
                    text=True,
                )
            else:
                process.terminate()
        except Exception:
            pass
    if started_infra:
        stop_compose_services(started_infra)
    raise
PY

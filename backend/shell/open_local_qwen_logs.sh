#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  bash shell/open_local_qwen_logs.sh [--tail-lines N]
EOF
}

TAIL_LINES=50

while [[ $# -gt 0 ]]; do
  case "$1" in
    --tail-lines)
      TAIL_LINES="${2:-}"
      shift 2
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

LOG_DIR="data/tmp"
PID_FILE="$LOG_DIR/local-qwen-stack.json"

if [[ -f "$PID_FILE" ]]; then
  tail -n "$TAIL_LINES" -F \
    "$LOG_DIR"/qwen-api.log \
    "$LOG_DIR"/qwen-recovery.log \
    "$LOG_DIR"/qwen-doc-worker.log \
    "$LOG_DIR"/qwen-finalize-worker.log \
    "$LOG_DIR"/qwen-infer-worker-*.log
else
  echo "No local qwen stack pid file found: $PID_FILE" >&2
  exit 1
fi

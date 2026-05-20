#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  bash shell/start_local_openai_stack_linux.sh [--conda-env NAME] [--worker-count N] [--worker-concurrency N] [--port N] [--dry-run]

Notes:
  - Linux physical server + conda environment bootstrapper.
  - OpenAI-only wrapper around start_local_all_stack_linux.sh.
EOF
}

ARGS=(--openai-only)

while [[ $# -gt 0 ]]; do
  case "$1" in
    --conda-env|--port)
      ARGS+=("$1" "${2:-}")
      shift 2
      ;;
    --worker-count)
      ARGS+=(--openai-worker-count "${2:-}")
      shift 2
      ;;
    --worker-concurrency)
      ARGS+=(--openai-worker-concurrency "${2:-}")
      shift 2
      ;;
    --dry-run)
      ARGS+=(--dry-run)
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
LOCAL_STACK_KIND=openai exec bash "$SCRIPT_DIR/start_local_all_stack_linux.sh" "${ARGS[@]}"

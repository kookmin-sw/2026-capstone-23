#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
BACKEND_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"
CONDA_ENV_NAME="${LOCAL_CONDA_ENV_NAME:-backend}"

# shellcheck source=local_stack_lib.sh
source "$SCRIPT_DIR/local_stack_lib.sh"

PYTHON_EXE="$(local_stack_resolve_conda_python "$CONDA_ENV_NAME")"
if [[ -z "$PYTHON_EXE" || ! -x "$PYTHON_EXE" ]]; then
  echo "failed to resolve python executable from conda env: $CONDA_ENV_NAME" >&2
  exit 1
fi

local_stack_stop_pid_files \
  "$PYTHON_EXE" \
  "$BACKEND_ROOT" \
  "data/tmp/local-all-stack.json" \
  "data/tmp/local-openai-stack.json" \
  "data/tmp/local-qwen-stack.json"

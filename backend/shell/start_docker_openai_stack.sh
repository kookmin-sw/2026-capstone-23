#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  bash shell/start_docker_openai_stack.sh [--worker-replicas N] [--worker-concurrency N] [--dry-run]
EOF
}

WORKER_REPLICAS=0
WORKER_CONCURRENCY=0
DRY_RUN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --worker-replicas)
      WORKER_REPLICAS="${2:-}"
      shift 2
      ;;
    --worker-concurrency)
      WORKER_CONCURRENCY="${2:-}"
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
REPO_ROOT="$(cd -- "$BACKEND_ROOT/.." && pwd -P)"
ENV_FILE="$BACKEND_ROOT/.env"

read_env_value() {
  local file="$1"
  local key="$2"
  [[ -f "$file" ]] || return 1
  local line current_key value
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    [[ -z "$line" || "$line" == \#* || "$line" != *=* ]] && continue
    current_key="${line%%=*}"
    value="${line#*=}"
    if [[ "$current_key" == "$key" ]]; then
      if [[ "${value:0:1}" == '"' && "${value: -1}" == '"' ]]; then
        value="${value:1:${#value}-2}"
      fi
      printf '%s\n' "$value"
      return 0
    fi
  done < "$file"
  return 1
}

resolve_setting() {
  local name="$1"
  local default="$2"
  if [[ -n "${!name:-}" ]]; then
    printf '%s\n' "${!name}"
    return
  fi
  local file_value
  if file_value="$(read_env_value "$ENV_FILE" "$name")"; then
    printf '%s\n' "$file_value"
    return
  fi
  printf '%s\n' "$default"
}

RESOLVED_REPLICAS="$WORKER_REPLICAS"
if [[ "$RESOLVED_REPLICAS" == "0" ]]; then
  RESOLVED_REPLICAS="$(resolve_setting OPENAI_WORKER_REPLICAS 2)"
fi

RESOLVED_CONCURRENCY="$WORKER_CONCURRENCY"
if [[ "$RESOLVED_CONCURRENCY" == "0" ]]; then
  RESOLVED_CONCURRENCY="$(resolve_setting OPENAI_WORKER_MAX_CONCURRENCY 2)"
fi

cd "$REPO_ROOT"
compose_base=(docker compose --env-file "$ENV_FILE" -f "$REPO_ROOT/docker-compose.yml")
infra_cmd=("${compose_base[@]}" up -d redis rabbitmq backend recovery)
worker_cmd=("${compose_base[@]}" up -d --scale "worker-openai=${RESOLVED_REPLICAS}" worker-openai)
status_cmd=("${compose_base[@]}" ps)

if [[ "$DRY_RUN" == "1" ]]; then
  echo "[dry-run] OPENAI_WORKER_MAX_CONCURRENCY=${RESOLVED_CONCURRENCY} ${infra_cmd[*]}"
  echo "[dry-run] OPENAI_WORKER_MAX_CONCURRENCY=${RESOLVED_CONCURRENCY} ${worker_cmd[*]}"
  echo "[dry-run] ${status_cmd[*]}"
  exit 0
fi

OPENAI_WORKER_MAX_CONCURRENCY="${RESOLVED_CONCURRENCY}" "${infra_cmd[@]}"
OPENAI_WORKER_MAX_CONCURRENCY="${RESOLVED_CONCURRENCY}" "${worker_cmd[@]}"
"${status_cmd[@]}"

echo "Docker OpenAI stack configured."
echo "worker-openai replicas: ${RESOLVED_REPLICAS}"
echo "worker-openai concurrency: ${RESOLVED_CONCURRENCY}"

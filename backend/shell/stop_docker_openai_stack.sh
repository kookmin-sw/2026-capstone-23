#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
BACKEND_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"
REPO_ROOT="$(cd -- "$BACKEND_ROOT/.." && pwd -P)"
cd "$REPO_ROOT"

docker compose --env-file "$BACKEND_ROOT/.env" -f "$REPO_ROOT/docker-compose.yml" stop backend worker-openai recovery redis rabbitmq

echo "Stopped Docker OpenAI stack services."

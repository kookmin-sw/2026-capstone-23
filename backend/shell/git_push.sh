#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
COMMIT_MESSAGE_FILE="$PROJECT_ROOT/commit_msg.txt"

cd "$PROJECT_ROOT"

echo "=== Adding all files ==="
git add -A

echo "=== Checking status ==="
git status --short | head -20

echo "=== Creating commit ==="
if [ -f "$COMMIT_MESSAGE_FILE" ]; then
    git commit -F "$COMMIT_MESSAGE_FILE" || git commit -m "Initial commit: Luminir Document Parser"
else
    git commit -m "Initial commit: Luminir Document Parser"
fi

echo "=== Pushing to GitHub ==="
git push -u origin main

echo "=== Done! ==="

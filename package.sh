#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_NAME="hermes-webui"
ZIP_PATH="${REPO_ROOT}/${REPO_NAME}.zip"
TIMESTAMP="$(date '+%Y.%m.%d %H:%M')"

printf '\n╔══════════════════════════════════════╗\n║   ⏰ 更新时间：%s      ║\n╚══════════════════════════════════════╝\n' \
  "$TIMESTAMP" > "${REPO_ROOT}/VERSION.txt"

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

rsync -a \
  --exclude='.git' \
  --exclude='.cursor' \
  --exclude='.vscode' \
  --exclude='.venv' \
  --exclude='node_modules' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='.DS_Store' \
  --exclude='.integration-test-workspace' \
  --exclude='.integration-test-state' \
  --exclude='.tmp-test-home' \
  --exclude='.pytest_cache' \
  --exclude='graphify-out' \
  --exclude='archive' \
  --exclude="${REPO_NAME}.zip" \
  "${REPO_ROOT}/" "${TMP_DIR}/${REPO_NAME}/"

rm -f "$ZIP_PATH"
(cd "$TMP_DIR" && zip -rq "$ZIP_PATH" "$REPO_NAME")

echo "VERSION.txt updated: ${TIMESTAMP}"
echo "Package created: ${ZIP_PATH}"

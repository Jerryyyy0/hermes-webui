#!/bin/sh
set -eu

mkdir -p "$HERMES_HOME" "$HERMES_WEBUI_STATE_DIR" "$HERMES_WEBUI_DEFAULT_WORKSPACE"
chown -R root:root "$HERMES_HOME" "$HERMES_WEBUI_DEFAULT_WORKSPACE"

browser_pid=""
webui_pid=""

cleanup() {
  for pid in "$webui_pid" "$browser_pid"; do
    if [ -n "$pid" ]; then
      kill -TERM "$pid" 2>/dev/null || true
    fi
  done
  for pid in "$webui_pid" "$browser_pid"; do
    if [ -n "$pid" ]; then
      wait "$pid" 2>/dev/null || true
    fi
  done
}

trap 'cleanup; exit 0' INT TERM HUP

# Keep the browser image's original entrypoint intact.  It owns Camofox,
# noVNC, and the browser runtime lifecycle.
/usr/local/bin/zhiling-camofox-entrypoint &
browser_pid=$!

# WebUI 与 Gateway 均以 root 运行，与浏览器服务和两个持久化卷保持同一权限模型。
HOME=/root "$HERMES_RUNTIME_PYTHON" /opt/hermes-webui/server.py &
webui_pid=$!

while :; do
  if ! kill -0 "$browser_pid" 2>/dev/null; then
    set +e
    wait "$browser_pid"
    status=$?
    set -e
    cleanup
    exit "$status"
  fi
  if ! kill -0 "$webui_pid" 2>/dev/null; then
    set +e
    wait "$webui_pid"
    status=$?
    set -e
    cleanup
    exit "$status"
  fi
  sleep 1
done

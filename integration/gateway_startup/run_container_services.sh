#!/usr/bin/env bash
set -Eeuo pipefail

HERMES_HOME="${HERMES_HOME:-/home/hermeswebui/.hermes}"
HERMES_HOME="${HERMES_HOME%/}"
HERMES_WEBUI_AGENT_DIR="${HERMES_WEBUI_AGENT_DIR:-${HERMES_HOME}/hermes-agent}"
HERMES_PYTHON="${HERMES_PYTHON_PATH:-/usr/local/bin/python3}"

readonly HERMES_HOME HERMES_WEBUI_AGENT_DIR HERMES_PYTHON

gateway_pids=()
gateway_names=()
cleanup_started=0

log() {
  printf '[hermes-container] %s\n' "$*"
}

fail() {
  printf '[hermes-container] ERROR: %s\n' "$*" >&2
  exit 1
}

require_layout() {
  [[ -f "${HERMES_WEBUI_AGENT_DIR}/hermes_cli/main.py" ]] ||
    fail "Hermes Agent not found: ${HERMES_WEBUI_AGENT_DIR}"
  [[ -x "${HERMES_PYTHON}" ]] ||
    fail "Python is not executable: ${HERMES_PYTHON}"
}

start_gateway() {
  local name="$1"
  local profile_home="$2"
  local log_dir="${profile_home}/logs"
  local log_file="${log_dir}/gateway.log"
  local pid

  mkdir -p "${log_dir}"

  if [[ "${name}" == "default" ]]; then
    log "starting default Gateway"
    (
      cd "${HERMES_WEBUI_AGENT_DIR}"
      exec > >(tee -a "${log_file}") 2>&1
      exec "${HERMES_PYTHON}" -m hermes_cli.main gateway run
    ) &
  else
    log "starting Gateway for profile: ${name}"
    (
      cd "${HERMES_WEBUI_AGENT_DIR}"
      exec > >(tee -a "${log_file}") 2>&1
      exec "${HERMES_PYTHON}" -m hermes_cli.main -p "${name}" gateway run
    ) &
  fi

  pid="$!"
  gateway_names+=("${name}")
  gateway_pids+=("${pid}")
}

stop_gateways() {
  local pid

  if [[ "${cleanup_started}" == "1" ]]; then
    return
  fi
  cleanup_started=1
  trap - TERM INT EXIT

  for pid in "${gateway_pids[@]}"; do
    if kill -0 "${pid}" 2>/dev/null; then
      kill -TERM "${pid}" 2>/dev/null || true
    fi
  done
  for pid in "${gateway_pids[@]}"; do
    wait "${pid}" 2>/dev/null || true
  done
}

handle_signal() {
  local signal_name="$1"
  log "received ${signal_name}; stopping Profile Gateways"
  stop_gateways
  if [[ "${signal_name}" == "INT" ]]; then
    exit 130
  fi
  exit 143
}

handle_exit() {
  local status="$1"
  stop_gateways
  exit "${status}"
}

require_layout
trap 'handle_signal TERM' TERM
trap 'handle_signal INT' INT
trap 'handle_exit $?' EXIT

profiles="$({
  cd "${HERMES_WEBUI_AGENT_DIR}"
  "${HERMES_PYTHON}" - <<'PY'
from hermes_cli.profiles import list_profiles

profiles = list_profiles()
default = next((profile for profile in profiles if profile.is_default), None)
multiplex = False
if default is not None:
    import yaml

    config_path = default.path / "config.yaml"
    if config_path.is_file():
        with config_path.open(encoding="utf-8") as stream:
            config = yaml.safe_load(stream) or {}
        gateway = config.get("gateway")
        nested = gateway.get("multiplex_profiles") if isinstance(gateway, dict) else None
        value = config.get("multiplex_profiles", nested)
        if isinstance(value, str):
            multiplex = value.strip().lower() in {"1", "true", "yes", "on"}
        else:
            multiplex = bool(value)

for profile in profiles:
    if multiplex and not profile.is_default:
        continue
    name = str(profile.name)
    path = str(profile.path)
    if any(separator in name or separator in path for separator in ("\t", "\n", "\r")):
        raise RuntimeError("Profile name/path contains unsupported control characters")
    print(name, path, str(bool(profile.gateway_running)).lower(), sep="\t")
PY
})"

while IFS=$'\t' read -r name profile_home already_running; do
  [[ -n "${name}" && -n "${profile_home}" ]] || continue
  if [[ "${already_running}" == "true" ]]; then
    log "Gateway already running: ${name}"
    continue
  fi
  start_gateway "${name}" "${profile_home}"
done <<<"${profiles}"

sleep "${HERMES_CONTAINER_GATEWAY_STARTUP_SECONDS:-3}"

startup_failed=0
for index in "${!gateway_pids[@]}"; do
  pid="${gateway_pids[$index]}"
  name="${gateway_names[$index]}"
  if kill -0 "${pid}" 2>/dev/null; then
    log "Gateway running: ${name} (PID ${pid})"
  else
    log "Gateway exited during startup: ${name} (PID ${pid})"
    startup_failed=1
  fi
done

if [[ "${startup_failed}" == "1" ]]; then
  fail "one or more Profile Gateways failed; inspect each profile logs/gateway.log"
fi

if [[ "${#gateway_pids[@]}" == "0" ]]; then
  log "all required Profile Gateways were already running"
  exit 0
fi

log "supervising ${#gateway_pids[@]} Profile Gateway process(es)"
set +e
wait -n "${gateway_pids[@]}"
gateway_status="$?"
set -e

fail "a Profile Gateway exited (status ${gateway_status}); stopping remaining Gateways"

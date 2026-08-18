from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "integration" / "gateway_startup" / "run_container_services.sh"


def test_container_service_script_passes_bash_syntax_check():
    completed = subprocess.run(
        ["bash", "-n", str(SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


def test_container_service_script_only_owns_gateway_lifecycle():
    source = SCRIPT.read_text(encoding="utf-8")

    assert 'from hermes_cli.profiles import list_profiles' in source
    assert '-p "${name}" gateway run' in source
    assert 'wait -n "${gateway_pids[@]}"' in source
    assert 'trap \'handle_signal TERM\' TERM' in source
    assert 'trap \'handle_signal INT\' INT' in source
    assert 'server.py' not in source
    assert 'HERMES_WEBUI_DIR' not in source
    assert 'HERMES_WEBUI_START_PROFILE_GATEWAYS' not in source
    assert 'ln -s' not in source
    assert '/app' not in source


def test_container_service_script_defaults_to_hermes_home_agent_layout():
    source = SCRIPT.read_text(encoding="utf-8")

    assert 'HERMES_WEBUI_AGENT_DIR="${HERMES_WEBUI_AGENT_DIR:-${HERMES_HOME}/hermes-agent}"' in source
    assert 'HERMES_PYTHON="${HERMES_PYTHON_PATH:-/usr/local/bin/python3}"' in source


def test_container_service_script_mirrors_gateway_output_to_console_and_log(tmp_path):
    wait_probe = subprocess.run(
        ["bash", "-c", "wait -n"],
        capture_output=True,
        text=True,
        check=False,
    )
    home = tmp_path / "hermes-home"
    agent_dir = home / "hermes-agent" / "hermes_cli"
    agent_dir.mkdir(parents=True)
    (agent_dir / "main.py").touch()
    fake_python = tmp_path / "fake-python"
    fake_python.write_text(
        """#!/usr/bin/env bash
set -eu
if [[ "${1:-}" == "-" ]]; then
  cat >/dev/null
  printf 'default\\t%s\\tfalse\\n' "${HERMES_HOME}"
  printf 'named\\t%s\\tfalse\\n' "${HERMES_HOME}/profiles/named"
  exit 0
fi
printf '%s\\n' "$*" >> "${HERMES_HOME}/gateway-argv"
printf 'gateway stdout\\n'
printf 'gateway stderr\\n' >&2
trap 'exit 0' TERM INT
while true; do sleep 1; done
""",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    env = {
        **os.environ,
        "HERMES_HOME": str(home),
        "HERMES_WEBUI_AGENT_DIR": str(home / "hermes-agent"),
        "HERMES_PYTHON_PATH": str(fake_python),
        "HERMES_CONTAINER_GATEWAY_STARTUP_SECONDS": "0",
    }
    if wait_probe.returncode == 2:
        bash_env = tmp_path / "bash-env"
        bash_env.write_text(
            """wait() {
  if [[ "${1:-}" == "-n" ]]; then shift; fi
  builtin wait "$@"
}
""",
            encoding="utf-8",
        )
        env["BASH_ENV"] = str(bash_env)
    proc = subprocess.Popen(
        ["bash", str(SCRIPT)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
        start_new_session=True,
    )
    log_file = home / "logs" / "gateway.log"
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if log_file.exists() and "gateway stderr" in log_file.read_text(encoding="utf-8"):
                break
            time.sleep(0.05)
        else:
            raise AssertionError("gateway output was not written to its log file")
    finally:
        proc.terminate()
        output, _ = proc.communicate(timeout=5)

    assert "gateway stdout" in output
    assert "gateway stderr" in output
    assert "gateway stdout" in log_file.read_text(encoding="utf-8")
    assert "gateway stderr" in log_file.read_text(encoding="utf-8")
    gateway_argv = (home / "gateway-argv").read_text(encoding="utf-8").splitlines()
    assert "-m hermes_cli.main gateway run -v" in gateway_argv
    assert "-m hermes_cli.main -p named gateway run -v" in gateway_argv

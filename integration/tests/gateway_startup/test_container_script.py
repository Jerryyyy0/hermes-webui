from __future__ import annotations

import subprocess
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

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


def test_container_service_script_leaves_gateway_ownership_to_server():
    completed = subprocess.run(
        ["bash", str(SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == "[hermes-container] Gateway startup is managed by server.py.\n"
    assert "gateway run" not in SCRIPT.read_text(encoding="utf-8")

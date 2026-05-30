"""Integration test environment (before api.config import side effects)."""

import os
from pathlib import Path

_ws = Path(__file__).resolve().parents[2] / ".integration-test-workspace"
_ws.mkdir(parents=True, exist_ok=True)
_state = Path(__file__).resolve().parents[2] / ".integration-test-state"
_state.mkdir(parents=True, exist_ok=True)
(_state / "sessions").mkdir(parents=True, exist_ok=True)

os.environ.setdefault("HERMES_WEBUI_DEFAULT_WORKSPACE", str(_ws))
os.environ.setdefault("HERMES_WEBUI_STATE_DIR", str(_state))
os.environ.setdefault("HERMES_HOME", str(_state / "hermes-home"))
os.environ.setdefault("HERMES_BASE_HOME", str(_state / "hermes-home"))

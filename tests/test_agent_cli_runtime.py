from pathlib import Path
from types import SimpleNamespace

import pytest

from api import agent_cli_runtime as runtime


def _ok(*_args, **_kwargs):
    return SimpleNamespace(returncode=0)


def test_explicit_launcher_wins_and_scrubs_python_overrides(monkeypatch, tmp_path):
    launcher = tmp_path / "hermes"
    launcher.write_text("", encoding="utf-8")
    launcher.chmod(0o755)
    monkeypatch.setenv("HERMES_WEBUI_HERMES_EXECUTABLE", str(launcher))
    monkeypatch.setenv("PYTHONPATH", "/wrong/source")
    monkeypatch.setenv("PYTHONHOME", "/wrong/home")

    resolved = runtime.resolve_agent_cli_runtime(agent_dir=None, probe_runner=_ok)

    assert resolved.command_prefix == (str(launcher),)
    assert resolved.kind == "explicit_launcher"
    assert "PYTHONPATH" not in resolved.env
    assert "PYTHONHOME" not in resolved.env


def test_managed_launcher_precedes_managed_python_and_path(monkeypatch, tmp_path):
    launcher = tmp_path / "venv" / "bin" / "hermes"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("", encoding="utf-8")
    launcher.chmod(0o755)
    python = launcher.parent / "python"
    python.write_text("", encoding="utf-8")
    python.chmod(0o755)
    monkeypatch.delenv("HERMES_WEBUI_HERMES_EXECUTABLE", raising=False)
    monkeypatch.setattr(runtime.shutil, "which", lambda _name: "/other/hermes")

    resolved = runtime.resolve_agent_cli_runtime(agent_dir=tmp_path, probe_runner=_ok)

    assert resolved.command_prefix == (str(launcher),)
    assert resolved.kind == "managed_launcher"


def test_missing_rich_rejects_python_and_fails_closed(monkeypatch, tmp_path):
    python = tmp_path / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    python.chmod(0o755)
    monkeypatch.delenv("HERMES_WEBUI_HERMES_EXECUTABLE", raising=False)
    monkeypatch.setattr(runtime.shutil, "which", lambda _name: None)

    def failed_probe(command, **_kwargs):
        assert "import hermes_cli.main, rich, yaml" in command
        return SimpleNamespace(returncode=1)

    with pytest.raises(runtime.AgentCliRuntimeUnavailable):
        runtime.resolve_agent_cli_runtime(agent_dir=tmp_path, probe_runner=failed_probe)


def test_first_candidate_failure_falls_back_to_managed_python(monkeypatch, tmp_path):
    launcher = tmp_path / "venv" / "bin" / "hermes"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("", encoding="utf-8")
    launcher.chmod(0o755)
    python = launcher.parent / "python"
    python.write_text("", encoding="utf-8")
    python.chmod(0o755)
    monkeypatch.delenv("HERMES_WEBUI_HERMES_EXECUTABLE", raising=False)
    monkeypatch.setattr(runtime.shutil, "which", lambda _name: None)

    def probe(command, **_kwargs):
        return SimpleNamespace(returncode=0 if command[0] == str(python) else 1)

    resolved = runtime.resolve_agent_cli_runtime(agent_dir=tmp_path, probe_runner=probe)

    assert resolved.command_prefix == (str(python), "-m", "hermes_cli.main")
    assert resolved.kind == "managed_python"


def test_gateway_command_profile_shape():
    invocation = runtime.AgentCliInvocation(("/hermes",), "/home", {}, "launcher")

    assert runtime.build_gateway_command(
        invocation, {"name": "default", "is_default": True}, "start"
    ) == ["/hermes", "gateway", "start"]
    assert runtime.build_gateway_command(
        invocation, {"name": "abc", "is_default": False}, "start"
    ) == ["/hermes", "-p", "abc", "gateway", "start"]


def test_relative_explicit_launcher_is_rejected(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_HERMES_EXECUTABLE", "relative/hermes")

    with pytest.raises(runtime.AgentCliRuntimeUnavailable):
        runtime.resolve_agent_cli_runtime(agent_dir=Path("/missing"), probe_runner=_ok)

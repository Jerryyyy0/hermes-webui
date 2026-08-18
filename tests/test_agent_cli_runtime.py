from pathlib import Path
from types import SimpleNamespace

import pytest

from integration.gateway_startup import runtime


def test_api_compatibility_exports_integration_runtime():
    from api import agent_cli_runtime as compatibility

    assert compatibility.AgentCliInvocation is runtime.AgentCliInvocation
    assert compatibility.AgentCliRuntimeUnavailable is runtime.AgentCliRuntimeUnavailable
    assert compatibility.resolve_agent_cli_runtime is runtime.resolve_agent_cli_runtime
    assert compatibility.build_gateway_command is runtime.build_gateway_command


def _ok(*_args, **_kwargs):
    return SimpleNamespace(returncode=0)


def test_agent_dir_prefers_explicit_override(monkeypatch, tmp_path):
    explicit = tmp_path / "explicit-agent"
    home_agent = tmp_path / "home" / "hermes-agent"
    for path in (explicit, home_agent):
        (path / "hermes_cli").mkdir(parents=True)
        (path / "hermes_cli" / "main.py").write_text("", encoding="utf-8")
    monkeypatch.setenv("HERMES_WEBUI_AGENT_DIR", str(explicit))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))

    assert runtime._agent_dir() == explicit.resolve()


def test_agent_dir_uses_hermes_home_without_webui_symlink(monkeypatch, tmp_path):
    hermes_home = tmp_path / ".hermes"
    agent = hermes_home / "hermes-agent"
    (agent / "hermes_cli").mkdir(parents=True)
    (agent / "hermes_cli" / "main.py").write_text("", encoding="utf-8")
    monkeypatch.delenv("HERMES_WEBUI_AGENT_DIR", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    assert runtime._agent_dir() == agent.resolve()
    assert not (hermes_home / "hermes-webui").exists()


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


def test_current_python_falls_back_to_discovered_agent_source_root(monkeypatch, tmp_path):
    python = tmp_path / "python"
    python.write_text("", encoding="utf-8")
    python.chmod(0o755)
    agent_dir = tmp_path / "hermes-agent"
    agent_dir.mkdir()
    monkeypatch.delenv("HERMES_WEBUI_HERMES_EXECUTABLE", raising=False)
    monkeypatch.setattr(runtime.sys, "executable", str(python))
    monkeypatch.setattr(runtime.shutil, "which", lambda _name: None)
    calls = []

    def probe(command, **kwargs):
        calls.append((command, kwargs))
        if command[0] != str(python):
            return SimpleNamespace(returncode=1)
        return SimpleNamespace(returncode=0)

    resolved = runtime.resolve_agent_cli_runtime(agent_dir=agent_dir, probe_runner=probe)

    assert resolved.kind == "agent_source_python"
    assert resolved.command_prefix == (str(python), "-m", "hermes_cli.main")
    assert resolved.cwd == str(agent_dir)
    source_calls = [call for call in calls if call[0][0] == str(python)]
    assert [call[0] for call in source_calls] == [
        [str(python), "-c", "import hermes_cli.main, rich, yaml"],
        [str(python), "-m", "hermes_cli.main", "--version"],
    ]
    assert all(call[1]["cwd"] == str(agent_dir) for call in source_calls)


def test_source_python_uses_repo_root_and_requires_imports_and_version(monkeypatch, tmp_path):
    python = tmp_path / "python"
    python.write_text("", encoding="utf-8")
    python.chmod(0o755)
    monkeypatch.delenv("HERMES_WEBUI_HERMES_EXECUTABLE", raising=False)
    monkeypatch.setattr(runtime, "_agent_dir", lambda: None)
    monkeypatch.setattr(runtime.sys, "executable", str(python))
    monkeypatch.setattr(runtime.shutil, "which", lambda _name: None)
    calls = []

    def probe(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0)

    resolved = runtime.resolve_agent_cli_runtime(agent_dir=None, probe_runner=probe)

    repo_root = str(Path(runtime.__file__).resolve().parents[2])
    assert resolved.kind == "source_python"
    assert resolved.command_prefix == (str(python), "-m", "hermes_cli.main")
    assert resolved.cwd == repo_root
    assert [call[0] for call in calls] == [
        [str(python), "-c", "import hermes_cli.main, rich, yaml"],
        [str(python), "-m", "hermes_cli.main", "--version"],
    ]
    assert all(call[1]["cwd"] == repo_root for call in calls)
    assert all("PYTHONPATH" not in call[1]["env"] for call in calls)


def test_source_python_import_failure_falls_back_to_path_launcher(monkeypatch, tmp_path):
    python = tmp_path / "python"
    launcher = tmp_path / "hermes"
    for executable in (python, launcher):
        executable.write_text("", encoding="utf-8")
        executable.chmod(0o755)
    monkeypatch.delenv("HERMES_WEBUI_HERMES_EXECUTABLE", raising=False)
    monkeypatch.setattr(runtime.sys, "executable", str(python))
    monkeypatch.setattr(runtime.shutil, "which", lambda _name: str(launcher))

    def probe(command, **_kwargs):
        return SimpleNamespace(returncode=0 if command[0] == str(launcher) else 1)

    resolved = runtime.resolve_agent_cli_runtime(agent_dir=None, probe_runner=probe)

    assert resolved.kind == "path_launcher"
    assert resolved.command_prefix == (str(launcher),)
    assert resolved.cwd == str(Path.home())


def test_source_python_version_failure_is_rejected(monkeypatch, tmp_path):
    python = tmp_path / "python"
    python.write_text("", encoding="utf-8")
    python.chmod(0o755)
    monkeypatch.delenv("HERMES_WEBUI_HERMES_EXECUTABLE", raising=False)
    monkeypatch.setattr(runtime.sys, "executable", str(python))
    monkeypatch.setattr(runtime.shutil, "which", lambda _name: None)

    def probe(command, **_kwargs):
        return SimpleNamespace(returncode=0 if "-c" in command else 1)

    with pytest.raises(runtime.AgentCliRuntimeUnavailable, match="source_python"):
        runtime.resolve_agent_cli_runtime(agent_dir=None, probe_runner=probe)


def test_source_python_builds_named_profile_gateway_command(monkeypatch, tmp_path):
    python = tmp_path / "python"
    python.write_text("", encoding="utf-8")
    python.chmod(0o755)
    monkeypatch.delenv("HERMES_WEBUI_HERMES_EXECUTABLE", raising=False)
    monkeypatch.setattr(runtime, "_agent_dir", lambda: None)
    monkeypatch.setattr(runtime.sys, "executable", str(python))
    monkeypatch.setattr(runtime.shutil, "which", lambda _name: None)

    resolved = runtime.resolve_agent_cli_runtime(agent_dir=None, probe_runner=_ok)

    assert runtime.build_gateway_command(
        resolved, {"name": "tiao-kong-zhu-li", "is_default": False}, "start"
    ) == [
        str(python),
        "-m",
        "hermes_cli.main",
        "-p",
        "tiao-kong-zhu-li",
        "gateway",
        "start",
    ]


def test_gateway_command_profile_shape():
    invocation = runtime.AgentCliInvocation(("/hermes",), "/home", {}, "launcher")

    assert runtime.build_gateway_command(
        invocation, {"name": "default", "is_default": True}, "start"
    ) == ["/hermes", "gateway", "start"]
    assert runtime.build_gateway_command(
        invocation, {"name": "abc", "is_default": False}, "start"
    ) == ["/hermes", "-p", "abc", "gateway", "start"]


def test_gateway_run_command_uses_foreground_external_supervisor_without_force():
    invocation = runtime.AgentCliInvocation(("/hermes",), "/home", {}, "launcher")

    assert runtime.build_gateway_run_command(
        invocation, {"name": "abc", "is_default": False}
    ) == ["/hermes", "-p", "abc", "gateway", "run", "-vv", "--external-supervisor"]


def test_agent_python_probe_uses_verified_python_runtime():
    invocation = runtime.AgentCliInvocation(("/agent-python", "-m", "hermes_cli.main"), "/home", {}, "source_python")

    assert runtime.build_agent_python_command(invocation, "-c", "print('ok')") == [
        "/agent-python",
        "-c",
        "print('ok')",
    ]


def test_agent_python_probe_does_not_guess_an_explicit_launcher():
    invocation = runtime.AgentCliInvocation(("/custom/hermes",), "/home", {}, "explicit_launcher")

    assert runtime.build_agent_python_command(invocation, "-c", "print('ok')") is None


def test_relative_explicit_launcher_is_rejected(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_HERMES_EXECUTABLE", "relative/hermes")

    with pytest.raises(runtime.AgentCliRuntimeUnavailable):
        runtime.resolve_agent_cli_runtime(agent_dir=Path("/missing"), probe_runner=_ok)

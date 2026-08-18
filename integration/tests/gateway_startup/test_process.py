from __future__ import annotations

import subprocess
import sys

import pytest

from integration.gateway_startup import process
from integration.gateway_startup.runtime import AgentCliInvocation


@pytest.fixture(autouse=True)
def reset_process_manager():
    process._reset_process_manager_for_tests()
    yield
    process.stop_gateway_processes(timeout=1)
    process._reset_process_manager_for_tests()


def _runtime(tmp_path):
    return AgentCliInvocation((sys.executable,), str(tmp_path), {"PYTHONUTF8": "1"}, "test")


def test_start_forwards_stdout_and_stderr_with_profile_prefix(monkeypatch, tmp_path):
    command = (
        "import sys; "
        "sys.stdout.write('DEBUG startup complete\\n'); sys.stdout.flush(); "
        "sys.stderr.write('WARNING retrying request\\n'); sys.stderr.flush()"
    )
    monkeypatch.setattr(process, "build_gateway_run_command", lambda _runtime, _profile: [sys.executable, "-u", "-c", command])
    lines = []
    monkeypatch.setattr(process, "log_gateway_line", lambda level, line: lines.append((level, line)))

    result = process.start_gateway_process(
        {"name": "coder", "path": str(tmp_path)},
        runtime=_runtime(tmp_path),
        state_probe=lambda _profile, _runtime: "not_running",
    )

    assert result["status"] == "started"
    entry = process._registry[str(tmp_path)]
    assert entry.wait_thread is not None
    entry.wait_thread.join(timeout=3)
    assert [line for _level, line in lines] == [
        "[gateway:coder] DEBUG startup complete",
        "[gateway:coder] WARNING retrying request",
    ]


def test_unknown_gateway_state_fails_closed_without_creating_process(tmp_path):
    def must_not_run(*_args, **_kwargs):
        pytest.fail("must not create a process when the Agent state is unknown")

    result = process.start_gateway_process(
        {"name": "default", "path": str(tmp_path)},
        runtime=_runtime(tmp_path),
        popen_factory=must_not_run,
        state_probe=lambda _profile, _runtime: "unknown",
    )

    assert result == {"profile": "default", "status": "state_unknown"}


def test_authoritative_probe_uses_agent_runtime_not_webui_import(monkeypatch, tmp_path):
    invocation = AgentCliInvocation(("/agent-python", "-m", "hermes_cli.main"), str(tmp_path), {"X": "1"}, "source_python")
    captured = {}

    def runner(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return subprocess.CompletedProcess(command, 0, stdout="not_running\n", stderr="")

    monkeypatch.setattr(process.subprocess, "run", runner)

    assert process.probe_profile_gateway_state({"path": str(tmp_path)}, invocation) == "not_running"
    assert captured["command"][:2] == ["/agent-python", "-c"]
    assert captured["cwd"] == str(tmp_path)
    assert captured["env"] == {"X": "1"}


def test_process_reader_redacts_before_console_emission(monkeypatch, tmp_path):
    command = "import sys; print('Authorization: Bearer top-secret-token')"
    monkeypatch.setattr(process, "build_gateway_run_command", lambda _runtime, _profile: [sys.executable, "-u", "-c", command])
    lines = []
    monkeypatch.setattr(process, "log_gateway_line", lambda _level, line: lines.append(line))

    process.start_gateway_process(
        {"name": "default", "path": str(tmp_path)},
        runtime=_runtime(tmp_path),
        state_probe=lambda _profile, _runtime: "not_running",
    )
    entry = process._registry[str(tmp_path)]
    assert entry.wait_thread is not None
    entry.wait_thread.join(timeout=3)

    assert lines == ["[gateway:default] Authorization: Bearer <redacted>"]
    assert "top-secret-token" not in lines[0]

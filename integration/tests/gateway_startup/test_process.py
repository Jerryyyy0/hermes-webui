from __future__ import annotations

import subprocess
import sys
import threading

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
        "sys.stderr.write('WARNING retrying request\\n'); sys.stderr.flush(); "
        "import time; time.sleep(0.2)"
    )
    monkeypatch.setattr(process, "build_gateway_run_command", lambda _runtime, _profile: [sys.executable, "-u", "-c", command])
    lines = []
    monkeypatch.setattr(process, "log_gateway_line", lambda level, line: lines.append((level, line)))

    result = process.start_gateway_process(
        {"name": "coder", "path": str(tmp_path)},
        runtime=_runtime(tmp_path),
        state_probe=lambda _profile, _runtime: "not_running",
        readiness_probe=lambda _entry: True,
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


def test_unknown_gateway_state_is_replaced_when_requested(monkeypatch, tmp_path):
    command = "import sys, time; print('INFO replacement after unknown state'); time.sleep(0.2)"
    replacements = []

    def build_command(_runtime, _profile, *, replace_existing=False):
        replacements.append(replace_existing)
        return [sys.executable, "-u", "-c", command]

    monkeypatch.setattr(process, "build_gateway_run_command", build_command)

    result = process.start_gateway_process(
        {"name": "default", "path": str(tmp_path)},
        runtime=_runtime(tmp_path),
        state_probe=lambda _profile, _runtime: "unknown",
        replace_existing=True,
        readiness_probe=lambda _entry: True,
    )

    assert result["status"] == "started"
    assert replacements == [True]


def test_running_gateway_is_not_replaced_by_default(monkeypatch, tmp_path):
    followed = []

    def must_not_run(*_args, **_kwargs):
        pytest.fail("must not replace an externally owned Gateway")

    monkeypatch.setattr(
        process,
        "follow_external_gateway_logs",
        lambda profile: followed.append(profile) or True,
    )
    result = process.start_gateway_process(
        {"name": "default", "path": str(tmp_path)},
        runtime=_runtime(tmp_path),
        popen_factory=must_not_run,
        state_probe=lambda _profile, _runtime: "running",
    )

    assert result == {"profile": "default", "status": "already_running"}
    assert followed == [{"name": "default", "path": str(tmp_path)}]


def test_external_gateway_follower_forwards_new_lines_without_replaying_history(monkeypatch, tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    gateway_log = logs / "gateway.log"
    error_log = logs / "gateway.error.log"
    gateway_log.write_text("INFO historical line\n", encoding="utf-8")
    error_log.write_text("WARNING historical warning\n", encoding="utf-8")
    lines = []
    seen = threading.Event()

    def capture(_level, line):
        lines.append(line)
        if len(lines) == 3:
            seen.set()

    monkeypatch.setattr(process, "log_gateway_line", capture)
    monkeypatch.setattr(process, "_EXTERNAL_LOG_POLL_SECONDS", 0.01)

    assert process.follow_external_gateway_logs({"name": "default", "path": str(tmp_path)})
    with gateway_log.open("a", encoding="utf-8") as log_file:
        log_file.write("INFO newly written\n")
    with error_log.open("a", encoding="utf-8") as log_file:
        log_file.write("ERROR newly written error\n")

    assert seen.wait(timeout=2)
    assert lines == [
        "[gateway:default] INFO WebUI attached to externally managed Gateway logs",
        "[gateway:default] INFO newly written",
        "[gateway:default] ERROR newly written error",
    ]


def test_running_gateway_is_replaced_when_requested(monkeypatch, tmp_path):
    command = "import sys, time; print('INFO replacement complete'); time.sleep(0.2)"
    replacements = []

    def build_command(_runtime, _profile, *, replace_existing=False):
        replacements.append(replace_existing)
        return [sys.executable, "-u", "-c", command]

    monkeypatch.setattr(process, "build_gateway_run_command", build_command)

    result = process.start_gateway_process(
        {"name": "default", "path": str(tmp_path)},
        runtime=_runtime(tmp_path),
        state_probe=lambda _profile, _runtime: "running",
        replace_existing=True,
        readiness_probe=lambda _entry: True,
    )

    assert result["status"] == "started"
    assert replacements == [True]


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


def test_owner_probe_uses_the_profile_home(monkeypatch, tmp_path):
    invocation = AgentCliInvocation(("/agent-python", "-m", "hermes_cli.main"), str(tmp_path), {"X": "1"}, "source_python")
    captured = {}

    def runner(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return subprocess.CompletedProcess(command, 0, stdout="managed\n", stderr="")

    monkeypatch.setattr(process.subprocess, "run", runner)

    assert process.probe_profile_gateway_owner({"path": str(tmp_path)}, invocation) == "managed"
    assert captured["command"][:2] == ["/agent-python", "-c"]
    assert captured["command"][-1] == str(tmp_path)
    assert "get_gateway_runtime_snapshot" in captured["command"][2]
    assert captured["cwd"] == str(tmp_path)
    assert captured["env"] == {"X": "1"}


def test_process_reader_redacts_before_console_emission(monkeypatch, tmp_path):
    command = "import sys, time; print('Authorization: Bearer top-secret-token'); time.sleep(0.2)"
    monkeypatch.setattr(process, "build_gateway_run_command", lambda _runtime, _profile: [sys.executable, "-u", "-c", command])
    lines = []
    monkeypatch.setattr(process, "log_gateway_line", lambda _level, line: lines.append(line))

    process.start_gateway_process(
        {"name": "default", "path": str(tmp_path)},
        runtime=_runtime(tmp_path),
        state_probe=lambda _profile, _runtime: "not_running",
        readiness_probe=lambda _entry: True,
    )
    entry = process._registry[str(tmp_path)]
    assert entry.wait_thread is not None
    entry.wait_thread.join(timeout=3)

    assert lines == ["[gateway:default] Authorization: Bearer <redacted>"]
    assert "top-secret-token" not in lines[0]


def test_start_waits_for_authoritative_readiness(monkeypatch, tmp_path):
    command = "import time; time.sleep(5)"
    monkeypatch.setattr(
        process,
        "build_gateway_run_command",
        lambda _runtime, _profile: [sys.executable, "-u", "-c", command],
    )

    result = process.start_gateway_process(
        {"name": "default", "path": str(tmp_path)},
        runtime=_runtime(tmp_path),
        state_probe=lambda _profile, _runtime: "not_running",
        readiness_probe=lambda entry: entry.process.poll() is None,
        readiness_timeout_seconds=0.5,
        readiness_poll_seconds=0.01,
    )

    assert result["status"] == "started"
    assert result["pid"] == process._registry[str(tmp_path)].process.pid


def test_start_fails_when_gateway_exits_before_readiness(monkeypatch, tmp_path):
    command = "import sys; sys.exit(7)"
    monkeypatch.setattr(
        process,
        "build_gateway_run_command",
        lambda _runtime, _profile: [sys.executable, "-u", "-c", command],
    )

    result = process.start_gateway_process(
        {"name": "default", "path": str(tmp_path)},
        runtime=_runtime(tmp_path),
        state_probe=lambda _profile, _runtime: "not_running",
        readiness_probe=lambda _entry: False,
        readiness_timeout_seconds=0.5,
        readiness_poll_seconds=0.01,
    )

    assert result == {"profile": "default", "status": "failed", "error": "exited_7"}


def test_start_terminates_a_gateway_that_never_becomes_ready(monkeypatch, tmp_path):
    command = "import time; time.sleep(5)"
    monkeypatch.setattr(
        process,
        "build_gateway_run_command",
        lambda _runtime, _profile: [sys.executable, "-u", "-c", command],
    )

    result = process.start_gateway_process(
        {"name": "default", "path": str(tmp_path)},
        runtime=_runtime(tmp_path),
        state_probe=lambda _profile, _runtime: "not_running",
        readiness_probe=lambda _entry: False,
        readiness_timeout_seconds=0.05,
        readiness_poll_seconds=0.01,
    )

    assert result == {"profile": "default", "status": "failed", "error": "readiness_timeout"}
    assert str(tmp_path) not in process._registry

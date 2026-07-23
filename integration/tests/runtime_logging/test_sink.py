from __future__ import annotations

import importlib
import io
import sys
import threading

import pytest


def _fresh_sink_module():
    import integration.runtime_logging.sink as sink

    return importlib.reload(sink)


@pytest.fixture
def sink_module(monkeypatch):
    original_stdout = sys.stdout
    original_stderr = sys.stderr
    sink = _fresh_sink_module()
    try:
        yield sink
    finally:
        sys.stdout = original_stdout
        sys.stderr = original_stderr
        setup = getattr(sink, "_SETUP", None)
        if setup and setup.crash_stream:
            try:
                setup.crash_stream.close()
            except Exception:
                pass
        sink = _fresh_sink_module()


def test_direct_entry_defaults_to_state_dir_log_and_crash_log(sink_module, tmp_path, monkeypatch):
    terminal = io.StringIO()
    monkeypatch.setattr(sys, "stdout", terminal)
    monkeypatch.setattr(sys, "stderr", terminal)

    setup = sink_module.setup_runtime_logging(state_dir=tmp_path, port=8787, enable=True)

    assert setup.enabled is True
    assert setup.log_path == tmp_path / "server-8787.log"
    assert setup.crash_log_path == tmp_path / "server-8787-crash.log"
    print("hello stdout")
    print("hello stderr", file=sys.stderr)
    setup.crash_stream.write("native crash line\n")
    setup.crash_stream.flush()

    assert "hello stdout" in terminal.getvalue()
    assert "hello stderr" in terminal.getvalue()
    text = setup.log_path.read_text(encoding="utf-8")
    assert "hello stdout" in text
    assert "hello stderr" in text
    assert setup.crash_log_path.read_text(encoding="utf-8") == "native crash line\n"


def test_non_interactive_stderr_skips_sink(sink_module, tmp_path, monkeypatch):
    terminal = io.StringIO()
    monkeypatch.setattr(sys, "stdout", terminal)
    monkeypatch.setattr(sys, "stderr", terminal)

    setup = sink_module.setup_runtime_logging(state_dir=tmp_path, port=8787)

    assert setup.enabled is False
    assert setup.reason == "not_interactive"
    assert not (tmp_path / "server-8787.log").exists()


def test_explicit_disable_avoids_log_files(sink_module, tmp_path, monkeypatch):
    terminal = io.StringIO()
    monkeypatch.setattr(sys, "stdout", terminal)
    monkeypatch.setattr(sys, "stderr", terminal)

    setup = sink_module.setup_runtime_logging(state_dir=tmp_path, port=8787, enable=False)

    assert setup.enabled is False
    assert setup.reason == "external"
    assert not (tmp_path / "server-8787.log").exists()


def test_custom_log_path_and_size_rotation(sink_module, tmp_path, monkeypatch):
    log_path = tmp_path / "custom" / "webui.log"
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    monkeypatch.setattr(sys, "stderr", io.StringIO())

    setup = sink_module.setup_runtime_logging(
        state_dir=tmp_path,
        port=9999,
        enable=True,
        log_path=log_path,
        max_bytes=80,
        backup_count=2,
    )
    for index in range(10):
        print(f"line-{index}-" + "x" * 40)

    assert setup.log_path == log_path
    assert log_path.exists()
    assert log_path.with_name("webui.log.1").exists()
    assert log_path.with_name("webui.log.2").exists()
    assert not log_path.with_name("webui.log.3").exists()


def test_concurrent_writes_are_serialized_to_single_sink(sink_module, tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    setup = sink_module.setup_runtime_logging(state_dir=tmp_path, port=8788, enable=True)

    def writer(prefix: str) -> None:
        for index in range(20):
            print(f"{prefix}-{index}")

    threads = [threading.Thread(target=writer, args=(f"t{idx}",)) for idx in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    text = setup.log_path.read_text(encoding="utf-8")
    for thread_index in range(4):
        for line_index in range(20):
            assert f"t{thread_index}-{line_index}" in text


def test_setup_is_idempotent(sink_module, tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    monkeypatch.setattr(sys, "stderr", io.StringIO())

    first = sink_module.setup_runtime_logging(state_dir=tmp_path, port=8787, enable=True)
    second = sink_module.setup_runtime_logging(state_dir=tmp_path, port=9999, enable=True)

    assert second is first
    assert second.log_path == tmp_path / "server-8787.log"


def test_open_failure_warns_and_continues_without_wrapping(sink_module, tmp_path, monkeypatch):
    existing_file = tmp_path / "not-a-dir"
    existing_file.write_text("x", encoding="utf-8")
    terminal = io.StringIO()
    monkeypatch.setattr(sys, "stderr", terminal)

    setup = sink_module.setup_runtime_logging(
        state_dir=tmp_path,
        port=8787,
        enable=True,
        log_path=existing_file / "webui.log",
    )

    assert setup.enabled is False
    assert setup.reason == "open_failed"
    assert "runtime log persistence disabled" in terminal.getvalue()
    assert sys.stderr is terminal


def test_crash_log_path_can_be_overridden(sink_module, tmp_path, monkeypatch):
    crash_path = tmp_path / "diagnostics" / "crash.log"
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    monkeypatch.setattr(sys, "stderr", io.StringIO())

    setup = sink_module.setup_runtime_logging(
        state_dir=tmp_path,
        port=8787,
        enable=True,
        crash_log_path=crash_path,
    )

    assert setup.crash_log_path == crash_path
    setup.crash_stream.write("crash\n")
    setup.crash_stream.flush()
    assert crash_path.read_text(encoding="utf-8") == "crash\n"

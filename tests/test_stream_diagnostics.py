import re

from api import stream_diagnostics as sd

_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} ")


def _without_ts(line: str) -> str:
    assert _TS_RE.match(line)
    return _TS_RE.sub("", line, count=1)


def test_stream_diag_disabled_by_default(monkeypatch, capsys):
    monkeypatch.delenv("HERMES_WEBUI_STREAM_DIAG", raising=False)

    payload = sd.log_event("webui.stream.open", "浏览器已连接聊天流。", stream_id="s1")

    assert payload["event"] == "webui.stream.open"
    assert payload["side"] == "webui"
    assert "[stream_diag]" not in capsys.readouterr().err


def test_stream_diag_marks_agent_side(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_STREAM_DIAG", "1")

    payload = sd.log_event("agent_init.basic", "Agent 基础初始化完成。", stream_id="s1")

    assert payload["side"] == "agent"


def test_stream_diag_logs_pretty_sanitized_line(monkeypatch, capsys):
    monkeypatch.setenv("HERMES_WEBUI_STREAM_DIAG", "1")

    payload = sd.log_event(
        "webui.worker.context_prepared",
        "上下文准备完成。",
        stream_id="stream-123456789",
        api_key="secret-value",
        message="x" * 260,
        elapsed_ms=12.3,
    )
    output = capsys.readouterr().err
    line = _without_ts(output.strip())

    assert payload["api_key"] == "[redacted]"
    assert "[stream_diag][webui][OK 12.3ms] 上下文准备完成。" in line
    assert "event=webui.worker.context_prepared" in line
    assert "stream=stream-1" in line
    assert "secret-value" not in output
    assert '"event"' not in output
    assert "x" * 241 not in output


def test_stream_diag_timing_fields_are_not_redacted(monkeypatch, capsys):
    monkeypatch.setenv("HERMES_WEBUI_STREAM_DIAG", "1")

    payload = sd.log_event(
        "agent.model_first_delta",
        "模型返回首个有效增量。",
        stream_id="s1",
        first_token_ms=123.4,
        first_visible_token_ms=456.7,
    )

    output = capsys.readouterr().err
    assert payload["first_token_ms"] == 123.4
    assert payload["first_visible_token_ms"] == 456.7
    assert "first_token=123.4ms" in output
    assert "first_visible_token=456.7ms" in output
    assert "[redacted]" not in output


def test_stream_diag_non_slow_events_are_emitted(monkeypatch, capsys):
    monkeypatch.setenv("HERMES_WEBUI_STREAM_DIAG", "1")

    sd.log_event(
        "webui.worker.agent_ready",
        "Agent 已准备就绪。",
        stream_id="s1",
        elapsed_ms=10.0,
    )

    line = _without_ts(capsys.readouterr().err.strip())
    assert "webui.worker.agent_ready" in line
    assert "[stream_diag][webui][OK 10.0ms]" in line


def test_stream_diag_debug_includes_debug_fields(monkeypatch, capsys):
    monkeypatch.setenv("HERMES_WEBUI_STREAM_DIAG", "debug")

    sd.log_event(
        "webui.worker.agent_import",
        "Agent 类加载自检完成。",
        stream_id="s1",
        agent_source_file="/tmp/hermes-agent/run_agent.py",
        event_callback_supported=True,
    )

    output = capsys.readouterr().err
    _without_ts(output.strip())
    assert "source=/tmp/hermes-agent/run_agent.py" in output
    assert "callback_supported=true" in output



def test_stream_diag_summary_and_event_counters(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_STREAM_DIAG", "1")
    sd.clear_stream_summary("stream-test")
    diag = sd.StreamDiag(stream_id="stream-test", session_id="session-test", workspace="/tmp/workspace")

    diag.note_model_first_delta()
    diag.note_queued_event("token")
    diag.note_queued_event("token")
    diag.note_queued_event("tool")
    summary = diag.summary_fields()
    summary_for_store = dict(summary)
    summary_for_store.pop("stream_id", None)
    sd.update_stream_summary("stream-test", **summary_for_store)
    stored = sd.get_stream_summary("stream-test")

    assert summary["events_enqueued"] == 3
    assert summary["event_counts"] == {"token": 2, "tool": 1}
    assert summary["first_token_ms"] is not None
    assert summary["first_visible_token_ms"] is not None
    assert stored["events_enqueued"] == 3
    assert stored["workspace_hash"]

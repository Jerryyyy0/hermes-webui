import re
import sys
import types

import pytest

from api.helpers import bad, j
from integration.project_logging.config import configure_logging
from integration.project_logging import emit_api_error, error_fields_from_payload, maybe_log_api_response
from integration.project_logging import request as log_policy


@pytest.fixture(autouse=True)
def _reset_logging_handlers():
    configure_logging(force=True)
    yield


class Headers:
    def __init__(self, values=None):
        self._values = values or {}

    def get(self, key, default=None):
        return self._values.get(key, default)


def _handler(**kwargs):
    defaults = {
        "command": "POST",
        "path": "/api/example?token=secret",
        "client_address": ("127.0.0.1", 12345),
        "headers": Headers(),
    }
    defaults.update(kwargs)
    return types.SimpleNamespace(**defaults)


_TS_RE = re.compile(
    r"^(?:(?:INFO|WARNING|ERROR|CRITICAL|DEBUG) )?"
    r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} "
)


def _webui_lines(output: str) -> list[str]:
    return [line.strip() for line in output.splitlines() if "[webui]" in line]


def _without_ts(line: str) -> str:
    assert _TS_RE.match(line)
    return _TS_RE.sub("", line, count=1)


def test_error_fields_from_payload_bad_shape():
    assert error_fields_from_payload({"error": "missing field"}) == (None, "missing field")
    assert error_fields_from_payload(
        {"error": "knowledge_base_upstream_failed", "message": "Connection refused"}
    ) == ("knowledge_base_upstream_failed", "Connection refused")
    assert error_fields_from_payload({"msg": "参数错误"}) == (None, "参数错误")


def test_bad_logs_structured_api_error(capsys, monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_LOG_LEVEL", "INFO")
    configure_logging(force=True)

    handler = _handler()
    sent = []

    def fake_send_response(status):
        sent.append(status)

    handler.send_response = fake_send_response
    handler.send_header = lambda *args, **kwargs: None
    handler.end_headers = lambda: None
    handler.wfile = types.SimpleNamespace(write=lambda _b: None)

    bad(handler, "missing field", 400)

    lines = _webui_lines(capsys.readouterr().err)
    assert len(lines) == 1
    line = _without_ts(lines[0])
    assert line.startswith("[webui][api_error] POST /api/example -> 400")
    assert "source=j" in line
    assert "remote=127.0.0.1" in line
    assert "message=\"missing field\"" in line
    assert "token=secret" not in line
    assert sent == [400]


def test_j_logs_502_with_traceback(capsys, monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_LOG_LEVEL", "INFO")
    configure_logging(force=True)

    handler = _handler()
    handler.send_response = lambda status: None
    handler.send_header = lambda *args, **kwargs: None
    handler.end_headers = lambda: None
    handler.wfile = types.SimpleNamespace(write=lambda _b: None)

    try:
        raise RuntimeError("upstream down")
    except RuntimeError as exc:
        j(
            handler,
            {"error": "upstream_failed", "message": str(exc)},
            status=502,
            exc_info=(type(exc), exc, exc.__traceback__),
        )

    output = capsys.readouterr().err
    line = _without_ts(_webui_lines(output)[0])
    assert line.startswith("[webui][api_error] POST /api/example -> 502")
    assert "error=upstream_failed" in line
    assert "message=\"upstream down\"" in line
    assert "traceback=yes" in line
    assert "RuntimeError" in output


def test_log_error_false_skips_logging(capsys, monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_LOG_LEVEL", "INFO")
    configure_logging(force=True)

    handler = _handler()
    handler.send_response = lambda status: None
    handler.send_header = lambda *args, **kwargs: None
    handler.end_headers = lambda: None
    handler.wfile = types.SimpleNamespace(write=lambda _b: None)

    bad(handler, "hidden", 500, log_error=False)

    assert _webui_lines(capsys.readouterr().err) == []


def test_api_error_hidden_by_error_level(capsys, monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_LOG_LEVEL", "ERROR")
    configure_logging(force=True)

    handler = _handler()
    emit_api_error(handler, status=400, message="should not log")

    assert _webui_lines(capsys.readouterr().err) == []


def test_api_error_404_hidden_at_critical_level(capsys, monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_LOG_LEVEL", "CRITICAL")
    configure_logging(force=True)

    handler = _handler()
    maybe_log_api_response(handler, 404, {"error": "not found"})

    assert _webui_lines(capsys.readouterr().err) == []


def test_emit_sets_handler_error_summary():
    handler = _handler()
    emit_api_error(handler, status=502, error_code="upstream_failed", message="down")
    assert handler._api_error_summary.startswith("502")


def test_server_log_request_includes_error_summary(capsys):
    from server import Handler

    handler = Handler.__new__(Handler)
    handler.command = "POST"
    handler.path = "/api/example"
    handler.client_address = ("127.0.0.1", 12345)
    handler.headers = Headers()
    handler._req_t0 = 0
    handler._api_error_summary = "502 | upstream_failed | down"

    Handler.log_request(handler, "502")

    raw = capsys.readouterr().err.strip()
    assert raw.startswith("INFO ")
    line = _without_ts(raw)
    assert line.startswith("POST /api/example -> 502")
    assert "error=\"502 | upstream_failed | down\"" in line


def test_server_unhandled_exception_logs_structured_error(capsys, monkeypatch):
    import server

    handler = types.SimpleNamespace(
        command="GET",
        path="/api/boom",
        client_address=("127.0.0.1", 1),
        headers=Headers(),
    )

    def boom_print(message):
        print(message, file=sys.stderr, flush=True)

    handler._safe_webui_print = boom_print

    def explode():
        raise ValueError("boom")

    monkeypatch.setattr(explode, "__name__", "explode")
    try:
        explode()
    except ValueError:
        server.Handler._log_unhandled_exception(handler)

    output = capsys.readouterr().err
    line = _without_ts(_webui_lines(output)[0])
    assert line.startswith("[webui][api_error] GET /api/boom -> 500")
    assert "source=unhandled" in line
    assert "traceback=yes" in line
    assert "ValueError" in output


def test_policy_helpers_defaults():
    assert log_policy.api_error_logging_enabled() is True
    assert log_policy.api_error_min_status() == 400
    assert log_policy.should_log_api_error(400) is True
    assert log_policy.should_log_api_error(200) is False

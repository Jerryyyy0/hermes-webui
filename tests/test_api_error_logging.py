import json
import sys
import types

import pytest

from api.helpers import bad, j
from integration.request_logging import emit_api_error, error_fields_from_payload, maybe_log_api_response
from integration.request_logging import policy as log_policy


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


def _parse_webui_lines(output: str) -> list[dict]:
    records = []
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("[webui] "):
            continue
        records.append(json.loads(line.removeprefix("[webui] ")))
    return records


def test_error_fields_from_payload_bad_shape():
    assert error_fields_from_payload({"error": "missing field"}) == (None, "missing field")
    assert error_fields_from_payload(
        {"error": "knowledge_base_upstream_failed", "message": "Connection refused"}
    ) == ("knowledge_base_upstream_failed", "Connection refused")
    assert error_fields_from_payload({"msg": "参数错误"}) == (None, "参数错误")


def test_bad_logs_structured_api_error(capsys, monkeypatch):
    monkeypatch.delenv("HERMES_WEBUI_API_ERROR_LOG", raising=False)
    monkeypatch.delenv("HERMES_WEBUI_API_ERROR_LOG_MIN_STATUS", raising=False)

    handler = _handler()
    sent = []

    def fake_send_response(status):
        sent.append(status)

    handler.send_response = fake_send_response
    handler.send_header = lambda *args, **kwargs: None
    handler.end_headers = lambda: None
    handler.wfile = types.SimpleNamespace(write=lambda _b: None)

    bad(handler, "missing field", 400)

    records = _parse_webui_lines(capsys.readouterr().err)
    assert len(records) == 1
    record = records[0]
    assert record["event"] == "api_error"
    assert record["status"] == 400
    assert record["path"] == "/api/example"
    assert "token=secret" not in record["path"]
    assert record["message"] == "missing field"
    assert sent == [400]


def test_j_logs_502_with_traceback(capsys, monkeypatch):
    monkeypatch.delenv("HERMES_WEBUI_API_ERROR_LOG", raising=False)

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

    record = _parse_webui_lines(capsys.readouterr().err)[0]
    assert record["status"] == 502
    assert record["error"] == "upstream_failed"
    assert "traceback" in record
    assert "RuntimeError" in record["traceback"]


def test_log_error_false_skips_logging(capsys, monkeypatch):
    monkeypatch.delenv("HERMES_WEBUI_API_ERROR_LOG", raising=False)

    handler = _handler()
    handler.send_response = lambda status: None
    handler.send_header = lambda *args, **kwargs: None
    handler.end_headers = lambda: None
    handler.wfile = types.SimpleNamespace(write=lambda _b: None)

    bad(handler, "hidden", 500, log_error=False)

    assert _parse_webui_lines(capsys.readouterr().err) == []


def test_api_error_logging_disabled_by_env(capsys, monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_API_ERROR_LOG", "0")

    handler = _handler()
    emit_api_error(handler, status=500, message="should not log")

    assert _parse_webui_lines(capsys.readouterr().err) == []


def test_api_error_min_status_env(capsys, monkeypatch):
    monkeypatch.delenv("HERMES_WEBUI_API_ERROR_LOG", raising=False)
    monkeypatch.setenv("HERMES_WEBUI_API_ERROR_LOG_MIN_STATUS", "500")

    handler = _handler()
    maybe_log_api_response(handler, 404, {"error": "not found"})

    assert _parse_webui_lines(capsys.readouterr().err) == []


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

    record = _parse_webui_lines(capsys.readouterr().out)[0]
    assert record["status"] == 502
    assert record["error_summary"] == "502 | upstream_failed | down"


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

    record = _parse_webui_lines(capsys.readouterr().err)[0]
    assert record["event"] == "api_error"
    assert record["source"] == "unhandled"
    assert record["status"] == 500
    assert "traceback" in record


def test_policy_helpers_defaults(monkeypatch):
    monkeypatch.delenv("HERMES_WEBUI_API_ERROR_LOG", raising=False)
    monkeypatch.delenv("HERMES_WEBUI_API_ERROR_LOG_MIN_STATUS", raising=False)
    assert log_policy.api_error_logging_enabled() is True
    assert log_policy.api_error_min_status() == 400
    assert log_policy.should_log_api_error(400) is True
    assert log_policy.should_log_api_error(200) is False

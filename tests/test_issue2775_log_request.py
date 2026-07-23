import logging
import re

import pytest

from integration.project_logging.config import configure_logging
from server import Handler

_TS_RE = re.compile(
    r"^(?:(?:INFO|WARNING|ERROR|CRITICAL|DEBUG) )?"
    r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} "
)


def _without_ts(line: str) -> str:
    assert _TS_RE.match(line)
    return _TS_RE.sub("", line, count=1)


@pytest.fixture(autouse=True)
def _reset_logging(capsys):
    configure_logging(level=logging.INFO, force=True)
    yield


def test_log_request_emits_at_info_without_error_summary(capsys):
    handler = Handler.__new__(Handler)
    handler.command = "GET"
    handler.path = "/api/session/manifest"
    handler.client_address = ("127.0.0.1", 12345)
    handler.headers = {}

    Handler.log_request(handler, "200")

    raw = capsys.readouterr().err.strip()
    assert raw.startswith("INFO ")
    line = _without_ts(raw)
    assert line.startswith("GET /api/session/manifest -> 200")
    assert "[webui][request]" not in line


def test_log_request_handles_malformed_request_without_path(capsys):
    """Malformed request lines can call log_request before path is assigned."""
    handler = Handler.__new__(Handler)
    handler.command = None

    Handler.log_request(handler, "400")

    raw = capsys.readouterr().err.strip()
    assert raw.startswith("INFO ")
    line = _without_ts(raw)
    assert line.startswith("- - -> 400")
    assert "remote" not in line
    assert "[webui][request]" not in line


def test_log_request_includes_remote_address(capsys):
    handler = Handler.__new__(Handler)
    handler.command = "POST"
    handler.path = "/api/auth/login"
    handler.client_address = ("192.0.2.10", 54321)
    handler.headers = {}

    Handler.log_request(handler, "401")

    raw = capsys.readouterr().err.strip()
    assert raw.startswith("INFO ")
    line = _without_ts(raw)
    assert line.startswith("POST /api/auth/login -> 401")
    assert "remote=192.0.2.10" in line
    assert "forwarded_for" not in line


def test_log_request_includes_first_forwarded_for_address(capsys):
    class Headers:
        def get(self, key):
            assert key == "X-Forwarded-For"
            return "203.0.113.7, 198.51.100.9"

    handler = Handler.__new__(Handler)
    handler.command = "POST"
    handler.path = "/api/auth/login"
    handler.client_address = ("192.0.2.10", 54321)
    handler.headers = Headers()

    Handler.log_request(handler, "401")

    raw = capsys.readouterr().err.strip()
    assert raw.startswith("INFO ")
    line = _without_ts(raw)
    assert line.startswith("POST /api/auth/login -> 401")
    assert "remote=192.0.2.10" in line
    assert "forwarded_for=203.0.113.7" in line

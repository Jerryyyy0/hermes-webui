import re

from server import Handler

_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} ")


def _without_ts(line: str) -> str:
    assert _TS_RE.match(line)
    return _TS_RE.sub("", line, count=1)


def test_log_request_handles_malformed_request_without_path(capsys):
    """Malformed request lines can call log_request before path is assigned."""
    handler = Handler.__new__(Handler)
    handler.command = None

    Handler.log_request(handler, "400")

    line = _without_ts(capsys.readouterr().err.strip())
    assert line.startswith("[webui][request] - - -> 400")
    assert "remote" not in line


def test_log_request_includes_remote_address(capsys):
    handler = Handler.__new__(Handler)
    handler.command = "POST"
    handler.path = "/api/auth/login"
    handler.client_address = ("192.0.2.10", 54321)
    handler.headers = {}

    Handler.log_request(handler, "401")

    line = _without_ts(capsys.readouterr().err.strip())
    assert line.startswith("[webui][request] POST /api/auth/login -> 401")
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

    line = _without_ts(capsys.readouterr().err.strip())
    assert line.startswith("[webui][request] POST /api/auth/login -> 401")
    assert "remote=192.0.2.10" in line
    assert "forwarded_for=203.0.113.7" in line

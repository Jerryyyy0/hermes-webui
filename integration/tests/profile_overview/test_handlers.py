from __future__ import annotations

from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch

from integration.profile_overview.handlers import try_handle_get


class DummyHandler:
    def __init__(self):
        self.wfile = BytesIO()
        self.status = None
        self.headers = {}

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.headers[name] = value

    def end_headers(self):
        pass


def _parsed(path: str):
    return SimpleNamespace(path=path, query="")


def test_overview_route_is_disabled_with_integration(monkeypatch):
    monkeypatch.delenv("HERMES_INTEGRATION", raising=False)

    assert try_handle_get(DummyHandler(), _parsed("/api/integration/profiles/p1/overview")) is False


def test_overview_route_returns_service_payload(monkeypatch):
    monkeypatch.setenv("HERMES_INTEGRATION", "1")
    payloads = []
    expected = {"profile": {"name": "p1"}}

    with patch("integration.profile_overview.handlers.build_overview", return_value=expected), patch(
        "integration.profile_overview.handlers.j",
        lambda _handler, payload, **_kwargs: payloads.append(payload),
    ):
        assert try_handle_get(DummyHandler(), _parsed("/api/integration/profiles/p1/overview")) is True

    assert payloads == [expected]


def test_raw_file_preview_and_download_use_fixed_file_type(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_INTEGRATION", "1")
    (tmp_path / "SOUL.md").write_text("# 助理", encoding="utf-8")
    profile = {"name": "p1"}
    payloads = []

    with patch("integration.profile_overview.handlers.resolve_profile", return_value=(profile, tmp_path)), patch(
        "integration.profile_overview.handlers.j",
        lambda _handler, payload, **_kwargs: payloads.append(payload),
    ):
        assert try_handle_get(
            DummyHandler(), _parsed("/api/integration/profiles/p1/raw_files/soul")
        ) is True

    assert payloads[0]["filename"] == "SOUL.md"
    assert payloads[0]["content"] == "# 助理"

    handler = DummyHandler()
    with patch("integration.profile_overview.handlers.resolve_profile", return_value=(profile, tmp_path)):
        assert try_handle_get(
            handler, _parsed("/api/integration/profiles/p1/raw_files/soul/download")
        ) is True

    assert handler.status == 200
    assert handler.headers["Content-Disposition"] == 'attachment; filename="SOUL.md"'
    assert handler.wfile.getvalue().decode("utf-8") == "# 助理"


def test_unknown_raw_file_type_returns_400(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_INTEGRATION", "1")
    calls = []

    with patch(
        "integration.profile_overview.handlers.resolve_profile",
        return_value=({"name": "p1"}, tmp_path),
    ), patch(
        "integration.profile_overview.handlers.bad",
        lambda _handler, message, status=400: calls.append((message, status)) or True,
    ):
        assert try_handle_get(
            DummyHandler(), _parsed("/api/integration/profiles/p1/raw_files/secret")
        ) is True

    assert calls == [("不支持的档案类型", 400)]

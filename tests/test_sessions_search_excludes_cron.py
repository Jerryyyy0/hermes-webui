from types import SimpleNamespace
from urllib.parse import urlparse


def _run_search(query, monkeypatch):
    import api.routes as routes

    rows = [
        {"session_id": "chat-1", "title": "html notes", "profile": "default"},
        {
            "session_id": "cron-tagged",
            "title": "html scheduled task",
            "profile": "default",
            "source_tag": "cron",
        },
        {
            "session_id": "cron_legacy_1",
            "title": "ordinary title",
            "profile": "default",
        },
    ]
    sessions = {
        "chat-1": SimpleNamespace(messages=[]),
        "cron-tagged": SimpleNamespace(messages=[]),
        "cron_legacy_1": SimpleNamespace(
            messages=[{"role": "user", "content": "html in scheduled output"}],
        ),
    }
    captured = {}

    def fake_j(handler, payload, status=200, extra_headers=None):
        captured["payload"] = payload

    monkeypatch.setattr(routes, "all_sessions", lambda: list(rows))
    monkeypatch.setattr(routes, "get_session_for_scan", lambda sid: sessions[sid])
    monkeypatch.setattr(routes, "load_settings", lambda: {"api_redact_enabled": False})
    monkeypatch.setattr(routes, "j", fake_j)
    import api.profiles

    monkeypatch.setattr(api.profiles, "get_active_profile_name", lambda: "default")
    routes._handle_sessions_search(SimpleNamespace(), urlparse(query))
    return captured["payload"]


def test_session_search_excludes_tagged_and_legacy_cron_sessions(monkeypatch):
    payload = _run_search(
        "/api/sessions/search?q=html&content=1&depth=0&all_profiles=1",
        monkeypatch,
    )

    assert [row["session_id"] for row in payload["sessions"]] == ["chat-1"]


def test_empty_session_search_excludes_cron_sessions(monkeypatch):
    payload = _run_search("/api/sessions/search?all_profiles=1", monkeypatch)

    assert [row["session_id"] for row in payload["sessions"]] == ["chat-1"]

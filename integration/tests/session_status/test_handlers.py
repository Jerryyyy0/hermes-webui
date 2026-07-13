from types import SimpleNamespace

from integration.session_status import handlers


class FakeHandler:
    def __init__(self):
        self.status = None
        self.payload = None


def test_mark_read_requires_session_id(monkeypatch):
    monkeypatch.setenv("HERMES_INTEGRATION", "1")
    captured = {}
    monkeypatch.setattr(handlers, "bad", lambda handler, msg, status=400: captured.update(msg=msg, status=status))
    assert handlers.try_handle_post(FakeHandler(), SimpleNamespace(path=handlers._PATH), {}) is True
    assert captured == {"msg": "session_id 必填", "status": 400}


def test_mark_read_uses_server_session(monkeypatch):
    monkeypatch.setenv("HERMES_INTEGRATION", "1")
    session = SimpleNamespace(session_id="s1")
    monkeypatch.setattr(handlers, "get_session", lambda sid: session)
    monkeypatch.setattr(handlers, "get_active_profile_name", lambda: "ops")
    monkeypatch.setattr(
        handlers,
        "mark_session_read",
        lambda profile, value: {"ok": True, "session_id": value.session_id, "read_until": 12},
    )
    captured = {}
    monkeypatch.setattr(handlers, "j", lambda handler, payload: captured.update(payload))
    body = {"session_id": "s1", "read_until": 999999}
    assert handlers.try_handle_post(FakeHandler(), SimpleNamespace(path=handlers._PATH), body) is True
    assert captured["read_until"] == 12

from api import routes


def test_enrich_adds_only_public_status_fields(monkeypatch):
    monkeypatch.setenv("HERMES_INTEGRATION", "1")
    monkeypatch.setattr(
        "integration.session_status.store.get_read_until_map",
        lambda rows: {("default", "s1"): 10},
    )
    rows = [{
        "session_id": "s1",
        "profile": "default",
        "last_message_at": 20,
        "last_error_at": None,
    }]
    routes._enrich_rows_with_session_status(rows)
    assert rows[0]["status"] == "has_new_messages"
    assert rows[0]["is_unread"] is True
    assert "last_error_at" not in rows[0]
    assert "read_until" not in rows[0]


def test_enrich_is_disabled_with_integration(monkeypatch):
    monkeypatch.delenv("HERMES_INTEGRATION", raising=False)
    rows = [{"session_id": "s1", "last_error_at": 20}]
    routes._enrich_rows_with_session_status(rows)
    assert "status" not in rows[0]
    assert "is_unread" not in rows[0]
    assert rows[0]["last_error_at"] == 20

    redacted = routes._redact_sidebar_session_rows(rows)
    assert "last_error_at" not in redacted[0]


def test_missing_cursor_defaults_old_session_to_read(monkeypatch):
    monkeypatch.setenv("HERMES_INTEGRATION", "1")
    monkeypatch.setattr("integration.session_status.store.get_read_until_map", lambda rows: {})
    rows = [{"session_id": "legacy", "last_message_at": 20}]
    routes._enrich_rows_with_session_status(rows)
    assert rows[0]["status"] == "ready"
    assert rows[0]["is_unread"] is False

from __future__ import annotations

import logging

import pytest

from integration.notifications import handlers as notification_handlers


def test_fetch_kb_messages_logs_without_sensitive_params(caplog, monkeypatch):
    monkeypatch.setattr(notification_handlers, "knowledge_base_enabled", lambda: True)

    def boom(*_args, **_kwargs):
        raise RuntimeError("downstream unavailable")

    monkeypatch.setattr(notification_handlers.kb_client, "post_json", boom)

    with caplog.at_level(logging.ERROR):
        rows = notification_handlers._fetch_kb_messages(
            account="user@example.com",
            uuid="user-uuid",
            read_type="all",
            limit=5,
        )

    assert rows == []
    joined = " ".join(record.message for record in caplog.records)
    assert "failed to fetch kb messages" in joined
    assert "user@example.com" not in joined
    assert "user-uuid" not in joined

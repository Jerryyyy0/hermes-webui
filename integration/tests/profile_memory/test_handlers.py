from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from integration.profile_memory.handlers import try_handle_get, try_handle_put


def _parsed(path: str, query: str = ""):
    return SimpleNamespace(path=path, query=query)


def test_routes_are_disabled_without_integration(monkeypatch):
    monkeypatch.delenv("HERMES_INTEGRATION", raising=False)

    assert try_handle_get(None, _parsed("/api/integration/profiles/p1/memory")) is False
    assert try_handle_put(None, _parsed("/api/integration/profiles/p1/memory"), {}) is False


def test_memory_get_and_timeline_forward_profile_and_pagination(monkeypatch):
    monkeypatch.setenv("HERMES_INTEGRATION", "1")
    payloads = []
    with patch(
        "integration.profile_memory.handlers.build_memory_overview",
        return_value={"profile": "p1"},
    ), patch(
        "integration.profile_memory.handlers.build_memory_timeline",
        return_value={"items": []},
    ) as timeline, patch(
        "integration.profile_memory.handlers.j",
        lambda _handler, payload, **_kwargs: payloads.append(payload),
    ):
        assert try_handle_get(None, _parsed("/api/integration/profiles/p1/memory")) is True
        assert try_handle_get(
            None,
            _parsed("/api/integration/profiles/p1/memory/timeline", "page=2&page_size=10"),
        ) is True

    timeline.assert_called_once_with("p1", page=2, page_size=10)
    assert payloads == [{"profile": "p1"}, {"items": []}]


def test_memory_put_updates_memory_document(monkeypatch):
    monkeypatch.setenv("HERMES_INTEGRATION", "1")
    payloads = []
    with patch(
        "integration.profile_memory.handlers.update_memory",
        return_value={"ok": True, "target": "memory"},
    ) as update, patch(
        "integration.profile_memory.handlers.j",
        lambda _handler, payload, **_kwargs: payloads.append(payload),
    ):
        assert try_handle_put(
            None,
            _parsed("/api/integration/profiles/p1/memory"),
            {"target": "memory", "content": "new content"},
        ) is True

    update.assert_called_once_with("p1", "memory", "new content")
    assert payloads == [{"ok": True, "target": "memory"}]


def test_invalid_pagination_returns_chinese_400(monkeypatch):
    monkeypatch.setenv("HERMES_INTEGRATION", "1")
    errors = []
    with patch(
        "integration.profile_memory.handlers.bad",
        lambda _handler, message, status=400: errors.append((message, status)),
    ):
        assert try_handle_get(
            None,
            _parsed("/api/integration/profiles/p1/memory/timeline", "page=x"),
        ) is True

    assert errors == [("page 必须是整数", 400)]

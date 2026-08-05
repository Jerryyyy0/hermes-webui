from types import SimpleNamespace
from unittest.mock import patch

from integration.common_tasks import generation, store
from integration.common_tasks.handlers import try_handle_get


class DummyHandler:
    pass


def _parsed(query: str):
    return SimpleNamespace(path="/api/integration/common_tasks", query=query)


def test_handler_returns_false_when_integration_disabled(monkeypatch):
    monkeypatch.setenv("HERMES_INTEGRATION", "0")
    assert try_handle_get(DummyHandler(), _parsed("profile=alice")) is False


def test_handler_returns_false_for_unmatched_path(monkeypatch):
    monkeypatch.setenv("HERMES_INTEGRATION", "1")
    other = SimpleNamespace(path="/api/integration/other", query="profile=alice")
    assert try_handle_get(DummyHandler(), other) is False


def test_handler_requires_profile(monkeypatch):
    monkeypatch.setenv("HERMES_INTEGRATION", "1")
    calls = []

    with patch(
        "integration.common_tasks.handlers.bad",
        lambda h, msg, status=400: calls.append((msg, status)),
    ):
        assert try_handle_get(DummyHandler(), _parsed("")) is True

    assert calls == [("profile 为必填参数", 400)]


def test_handler_profile_not_found_returns_404(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_INTEGRATION", "1")
    generation.disable_worker_for_tests(True)
    calls = []

    with patch(
        "integration.common_tasks.collectors.resolve_profile", return_value=None
    ), patch(
        "integration.common_tasks.handlers.bad",
        lambda h, msg, status=400: calls.append((msg, status)),
    ):
        assert try_handle_get(DummyHandler(), _parsed("profile=ghost")) is True

    assert calls == [("Profile 不存在", 404)]


def test_handler_cache_status_empty_when_no_tasks(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_INTEGRATION", "1")
    generation.disable_worker_for_tests(True)
    profile = tmp_path / "profile"
    profile.mkdir()
    payloads = []

    with patch(
        "integration.common_tasks.collectors.resolve_profile",
        return_value={"name": "alice", "path": str(profile)},
    ), patch(
        "integration.common_tasks.handlers.j", lambda h, payload: payloads.append(payload)
    ), patch("integration.common_tasks.generation.enqueue_missing_or_stale"):
        assert try_handle_get(DummyHandler(), _parsed("profile=alice")) is True

    payload = payloads[0]
    assert payload["profile"] == "alice"
    assert payload["cache_status"] == "empty"
    assert payload["items"] == []


def test_handler_cache_status_seed_when_only_seed_present(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_INTEGRATION", "1")
    generation.disable_worker_for_tests(True)
    profile = tmp_path / "profile"
    profile.mkdir()
    store.write_seed_tasks(
        profile,
        [
            {"title": "种子1", "trigger_language": "t1"},
            {"title": "种子2", "trigger_language": "t2"},
            {"title": "种子3", "trigger_language": "t3"},
        ],
    )
    payloads = []

    with patch(
        "integration.common_tasks.collectors.resolve_profile",
        return_value={"name": "alice", "path": str(profile)},
    ), patch(
        "integration.common_tasks.handlers.j", lambda h, payload: payloads.append(payload)
    ), patch("integration.common_tasks.generation.enqueue_missing_or_stale"):
        assert try_handle_get(DummyHandler(), _parsed("profile=alice")) is True

    payload = payloads[0]
    assert payload["cache_status"] == "seed"
    assert [item["title"] for item in payload["items"]] == ["种子1", "种子2", "种子3"]


def test_handler_cache_status_hit_when_mined_present(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_INTEGRATION", "1")
    generation.disable_worker_for_tests(True)
    profile = tmp_path / "profile"
    profile.mkdir()
    store.write_seed_tasks(
        profile,
        [{"title": "种子1", "trigger_language": "t1"}],
    )
    store.replace_mined_tasks(
        profile,
        [
            {
                "title": "挖掘1",
                "description": "d",
                "trigger_language": "tm",
                "query_count": 5,
                "members_json": "[]",
            }
        ],
        "fp-1",
        success=True,
    )
    payloads = []

    with patch(
        "integration.common_tasks.collectors.resolve_profile",
        return_value={"name": "alice", "path": str(profile)},
    ), patch(
        "integration.common_tasks.handlers.j", lambda h, payload: payloads.append(payload)
    ), patch("integration.common_tasks.generation.enqueue_missing_or_stale"):
        assert try_handle_get(DummyHandler(), _parsed("profile=alice")) is True

    payload = payloads[0]
    assert payload["cache_status"] == "hit"
    assert payload["items"][0]["title"] == "挖掘1"
    assert payload["items"][0]["source"] == "mined"


def test_handler_calls_enqueue_missing_or_stale(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_INTEGRATION", "1")
    generation.disable_worker_for_tests(True)
    profile = tmp_path / "profile"
    profile.mkdir()
    enqueued = []

    with patch(
        "integration.common_tasks.collectors.resolve_profile",
        return_value={"name": "alice", "path": str(profile)},
    ), patch(
        "integration.common_tasks.handlers.j", lambda h, payload: None
    ), patch(
        "integration.common_tasks.generation.enqueue_missing_or_stale",
        lambda profile_name, profile_path: enqueued.append((profile_name, profile_path)),
    ):
        assert try_handle_get(DummyHandler(), _parsed("profile=alice")) is True

    assert len(enqueued) == 1
    assert enqueued[0][0] == "alice"


def test_handler_payload_items_have_required_fields(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_INTEGRATION", "1")
    generation.disable_worker_for_tests(True)
    profile = tmp_path / "profile"
    profile.mkdir()
    store.write_seed_tasks(
        profile,
        [{"title": "种子1", "trigger_language": "t1"}],
    )
    payloads = []

    with patch(
        "integration.common_tasks.collectors.resolve_profile",
        return_value={"name": "alice", "path": str(profile)},
    ), patch(
        "integration.common_tasks.handlers.j", lambda h, payload: payloads.append(payload)
    ), patch("integration.common_tasks.generation.enqueue_missing_or_stale"):
        try_handle_get(DummyHandler(), _parsed("profile=alice"))

    item = payloads[0]["items"][0]
    assert set(item.keys()) == {"title", "description", "trigger_language", "query_count", "source"}
    assert item["source"] == "seed"
    assert item["query_count"] == 0
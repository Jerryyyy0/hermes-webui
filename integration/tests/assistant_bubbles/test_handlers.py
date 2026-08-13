import json
from types import SimpleNamespace
from unittest.mock import patch

from integration.assistant_bubbles import generation, store
from integration.assistant_bubbles.handlers import try_handle_get


class DummyHandler:
    pass


def _parsed(query: str):
    return SimpleNamespace(path="/api/integration/assistant_bubbles", query=query)


def test_handler_requires_profile(monkeypatch):
    monkeypatch.setenv("HERMES_INTEGRATION", "1")
    calls = []

    with patch("integration.assistant_bubbles.handlers.bad", lambda h, msg, status=400: calls.append((msg, status))):
        assert try_handle_get(DummyHandler(), _parsed("")) is True

    assert calls == [("profile 为必填参数", 400)]


def test_handler_resolves_profile_from_list_and_returns_scheduled_task(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_INTEGRATION", "1")
    generation.disable_worker_for_tests(True)
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "info.json").write_text(json.dumps({"display_name": "小助理", "description": "写文档"}), encoding="utf-8")
    cron = profile / "cron"
    cron.mkdir()
    (cron / "jobs.json").write_text(
        json.dumps({"jobs": [{"id": "1", "enabled": True}, {"id": "2", "last_status": "failed"}]}),
        encoding="utf-8",
    )
    data = store.empty_store()
    texts = ["我是小助理。", "心情不错。", None, "准备好了。", "记忆有更新。", "专注中。", "我会写文档。", "随时待命。"]
    for item, text in zip(data["items"], texts):
        if item["type"] != "scheduled_task":
            item["text"] = text
    store.write_store(profile, data)
    payloads = []

    with patch("api.profiles.list_profiles_api", return_value=[{"name": "alice", "path": str(profile)}]), patch(
        "integration.assistant_bubbles.handlers.j", lambda h, payload: payloads.append(payload)
    ), patch(
        "integration.assistant_bubbles.handlers.collectors.collect_context",
        side_effect=AssertionError("缓存命中不应同步收集气泡上下文"),
    ):
        assert try_handle_get(DummyHandler(), _parsed("profile=alice")) is True

    payload = payloads[0]
    assert payload["profile"] == "alice"
    assert payload["cache_status"] == "hit"
    assert [item["type"] for item in payload["items"]] == store.ITEM_ORDER
    assert "dynamic" not in payload["items"][2]
    assert "2个定时任务" in payload["items"][2]["text"]


def test_handler_bad_cache_falls_back_and_schedules_refresh(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_INTEGRATION", "1")
    generation.disable_worker_for_tests(True)
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "assistant_bubbles.json").write_text("not json", encoding="utf-8")
    payloads = []
    with patch("api.profiles.list_profiles_api", return_value=[{"name": "alice", "path": str(profile)}]), patch(
        "integration.assistant_bubbles.handlers.j", lambda h, payload: payloads.append(payload)
    ), patch("integration.assistant_bubbles.generation.enqueue_missing_or_stale") as schedule_refresh:
        assert try_handle_get(DummyHandler(), _parsed("profile=alice")) is True

    assert payloads[0]["cache_status"] == "fallback"
    assert len(payloads[0]["items"]) == 8
    schedule_refresh.assert_called_once_with("alice", profile, None)

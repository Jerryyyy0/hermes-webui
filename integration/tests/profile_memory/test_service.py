from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from integration.profile_memory import service


SHANGHAI = ZoneInfo("Asia/Shanghai")


def _profile_home(tmp_path, monkeypatch):
    profile = {"name": "p1", "info": {"display_name": "采购助理"}}
    monkeypatch.setattr(service, "resolve_profile", lambda _name: (profile, tmp_path))
    (tmp_path / "memories").mkdir()
    return profile


def test_overview_returns_only_memory_without_classifying(tmp_path, monkeypatch):
    _profile_home(tmp_path, monkeypatch)
    memory_dir = tmp_path / "memories"
    (memory_dir / "USER.md").write_text("user preference", encoding="utf-8")
    (memory_dir / "MEMORY.md").write_text("agent fact", encoding="utf-8")

    result = service.build_memory_overview("p1")

    assert result["assistant_name"] == "采购助理"
    assert set(result["memories"]) == {"memory"}
    assert result["memories"]["memory"]["content"] == "agent fact"
    assert result["updated_at"] is not None


def test_update_records_each_created_updated_and_deleted_entry(tmp_path, monkeypatch):
    _profile_home(tmp_path, monkeypatch)
    target = tmp_path / "memories" / "MEMORY.md"
    target.write_text("A\n§\nB", encoding="utf-8")
    now = datetime(2026, 9, 12, 11, 30, 20, tzinfo=SHANGHAI)

    result = service.update_memory("p1", "memory", "A\n§\nB changed\n§\nC", now=now)

    assert result["content"] == "A\n§\nB changed\n§\nC"
    events = service.read_memory_events(tmp_path)
    assert [(event["action"], event["content"]) for event in events] == [
        ("updated", "B changed"),
        ("created", "C"),
    ]
    assert {event["source"] for event in events} == {"webui"}

    service.update_memory("p1", "memory", "", now=now)
    deleted = service.read_memory_events(tmp_path)[-3:]
    assert [(event["action"], event["content"]) for event in deleted] == [
        ("deleted", "A"),
        ("deleted", "B changed"),
        ("deleted", "C"),
    ]


def test_timeline_is_reverse_chronological_and_paginated(tmp_path, monkeypatch):
    _profile_home(tmp_path, monkeypatch)
    journal = tmp_path / "memories" / "memory_events.jsonl"
    rows = [
        {
            "event_id": f"event-{index}",
            "target": "memory",
            "action": "created",
            "occurred_at": f"2026-09-12T10:{index:02d}:00+08:00",
            "content": f"memory {index}",
            "source": "agent",
        }
        for index in range(25)
    ]
    rows.append(
        {
            "event_id": "ignored-user-event",
            "target": "user",
            "action": "created",
            "occurred_at": "2026-09-12T11:00:00+08:00",
            "content": "must not be returned",
            "source": "agent",
        }
    )
    journal.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )

    result = service.build_memory_timeline("p1", page=1, page_size=20)

    assert result["pagination"] == {
        "page": 1,
        "page_size": 20,
        "total": 25,
        "has_more": True,
    }
    assert result["items"][0]["content"] == "memory 24"
    assert result["items"][-1]["content"] == "memory 5"
    assert result["items"][0]["event_name"] == "形成记忆"


def test_empty_history_is_allowed(tmp_path, monkeypatch):
    _profile_home(tmp_path, monkeypatch)

    result = service.build_memory_timeline("p1", page=1, page_size=20)

    assert result["items"] == []
    assert result["pagination"]["total"] == 0


def test_overview_uses_latest_memory_event_and_ignores_user_events(tmp_path, monkeypatch):
    _profile_home(tmp_path, monkeypatch)
    journal = tmp_path / "memories" / "memory_events.jsonl"
    journal.write_text(
        "".join(
            json.dumps(row) + "\n"
            for row in [
                {
                    "event_id": "memory-event",
                    "target": "memory",
                    "action": "deleted",
                    "occurred_at": "2026-09-12T13:05:10+08:00",
                    "content": "deleted memory",
                    "source": "agent",
                },
                {
                    "event_id": "newer-user-event",
                    "target": "user",
                    "action": "created",
                    "occurred_at": "2026-09-12T14:05:10+08:00",
                    "content": "ignored user profile",
                    "source": "agent",
                },
            ]
        ),
        encoding="utf-8",
    )

    result = service.build_memory_overview("p1")

    assert result["updated_at"] == "2026-09-12T13:05:10+08:00"


def test_update_honors_profile_memory_limit_override(tmp_path, monkeypatch):
    _profile_home(tmp_path, monkeypatch)
    (tmp_path / "config.yaml").write_text(
        "memory:\n  memory_char_limit: 4\n",
        encoding="utf-8",
    )

    try:
        service.update_memory("p1", "memory", "12345")
    except ValueError as exc:
        assert str(exc) == "MEMORY.md 内容不能超过 4 个字符"
    else:
        raise AssertionError("configured user memory limit was not enforced")


def test_update_rolls_back_file_when_event_append_fails(tmp_path, monkeypatch):
    _profile_home(tmp_path, monkeypatch)
    target = tmp_path / "memories" / "MEMORY.md"
    target.write_text("before", encoding="utf-8")
    monkeypatch.setattr(
        service,
        "_append_memory_events",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(OSError, match="disk full"):
        service.update_memory("p1", "memory", "after")

    assert target.read_text(encoding="utf-8") == "before"
    assert not (tmp_path / "memories" / "memory_events.jsonl").exists()


def test_update_rejects_user_profile_target(tmp_path, monkeypatch):
    _profile_home(tmp_path, monkeypatch)

    with pytest.raises(ValueError, match="target 只能是 memory"):
        service.update_memory("p1", "user", "must remain untouched")

    assert not (tmp_path / "memories" / "USER.md").exists()

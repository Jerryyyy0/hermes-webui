from __future__ import annotations

from collections import Counter
from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from integration.profile_overview import service


SHANGHAI = ZoneInfo("Asia/Shanghai")


def test_activity_covers_rolling_year_and_builds_monday_weeks():
    activity = service.build_activity(
        Counter({date(2026, 9, 11): 10, date(2026, 9, 10): 1}),
        date(2026, 9, 11),
    )

    assert activity["start_date"] == "2025-09-12"
    assert activity["end_date"] == "2026-09-11"
    assert len(activity["days"]) == 365
    assert activity["weeks"][0]["week_start"] == "2025-09-08"
    assert all(datetime.fromisoformat(w["week_start"]).weekday() == 0 for w in activity["weeks"])
    assert activity["days"][-1]["count"] == 10
    assert activity["days"][-1]["level"] == 4
    assert activity["days"][-2]["count"] == 1
    assert [item["month"] for item in activity["month_labels"]][0] == "2025-09"
    assert [item["month"] for item in activity["month_labels"]][-1] == "2026-09"


def test_activity_empty_days_use_zero_level_without_division_by_zero():
    activity = service.build_activity(Counter(), date(2026, 9, 11))

    assert activity["user_anchor"] == 0
    assert {day["ratio"] for day in activity["days"]} == {0.0}
    assert {day["level"] for day in activity["days"]} == {0}


def test_collect_sessions_counts_only_webui_and_cron_final_replies(monkeypatch):
    rows = [
        {"session_id": "chat-1", "profile": "p1", "source_tag": "webui"},
        {"session_id": "cron_job_1", "profile": "p1", "source_tag": "cron"},
        {"session_id": "legacy-cli", "profile": "p1", "source_tag": ""},
        {"session_id": "hook-1", "profile": "p1", "source_tag": "webhook"},
        {"session_id": "other-profile", "profile": "p2", "source_tag": "webui"},
    ]
    sessions = {
        "chat-1": SimpleNamespace(
            messages=[
                {"role": "user", "content": "hi", "timestamp": 1789056000},
                {"role": "assistant", "content": "ok", "timestamp": 1789056060},
                {"role": "assistant", "content": "", "timestamp": 1789056070},
                {"role": "assistant", "content": "failed", "timestamp": 1789056080, "_error": True},
                {"role": "assistant", "content": "tool", "timestamp": 1789056090, "tool_calls": [{}]},
                {"role": "assistant", "content": "partial", "timestamp": 1789056100, "_partial": True},
                {"role": "assistant", "content": "incomplete", "timestamp": 1789056110, "finish_reason": "incomplete"},
            ],
            source_tag="webui",
            async_delegation_origins={},
        ),
        "cron_job_1": SimpleNamespace(
            messages=[{"role": "assistant", "content": "done", "timestamp": 1789056060}],
            source_tag="cron",
            async_delegation_origins={},
        ),
        "legacy-cli": SimpleNamespace(
            messages=[{"role": "assistant", "content": "cli", "timestamp": 1789056060}],
            source_tag="cli",
            async_delegation_origins={},
        ),
    }
    monkeypatch.setattr("api.models.all_sessions", lambda **_kwargs: rows)
    monkeypatch.setattr("api.models.get_session_for_scan", lambda sid: sessions.get(sid))
    monkeypatch.setattr(service, "_profiles_match", lambda left, right: left == right)

    count, daily = service._collect_sessions("p1", date(2026, 9, 1), date(2026, 9, 30))

    assert count == 1
    assert sum(daily.values()) == 2


def test_collect_cron_count_only_includes_scheduled_or_running_jobs(monkeypatch, tmp_path):
    jobs = [
        {"id": "scheduled", "enabled": True, "state": "scheduled"},
        {"id": "running", "enabled": True, "state": "running"},
        {"id": "legacy", "enabled": True},
        {"id": "paused", "enabled": False, "state": "paused"},
        {"id": "disabled-scheduled", "enabled": False, "state": "scheduled"},
        {"id": "completed", "enabled": False, "state": "completed"},
        {"id": "failed", "enabled": False, "state": "failed"},
    ]
    monkeypatch.setattr("cron.jobs.list_jobs", lambda **_kwargs: jobs)

    assert service._collect_cron_count(tmp_path) == 3


def test_recent_learned_includes_disabled_and_keeps_latest_three(monkeypatch, tmp_path):
    skills = [
        {"name": f"s{i}", "full_name": f"s{i}", "disabled": i == 1, "hub_installed": i < 3}
        for i in range(5)
    ]
    mtimes = {
        "s0": datetime(2026, 9, 11, 10, tzinfo=SHANGHAI).timestamp(),
        "s1": datetime(2026, 9, 12, 9, tzinfo=SHANGHAI).timestamp(),
        "s2": datetime(2026, 9, 10, 8, tzinfo=SHANGHAI).timestamp(),
        "s3": datetime(2026, 9, 9, 7, tzinfo=SHANGHAI).timestamp(),
        "s4": datetime(2026, 9, 1, 7, tzinfo=SHANGHAI).timestamp(),
    }
    monkeypatch.setattr(service, "skills_dir_for_profile", lambda _profile: tmp_path)
    monkeypatch.setattr(service, "list_installed", lambda _profile: {"skills": skills})
    monkeypatch.setattr(
        service,
        "read_local_skill_mtime",
        lambda _root, full_name: mtimes[full_name],
    )

    recent = service.list_recent_learned_skills("p1", date(2026, 9, 12))

    assert [item["name"] for item in recent] == ["s1", "s0", "s2"]
    assert recent[0]["disabled"] is True


def test_recent_learned_excludes_manual_upload_and_system_skill(monkeypatch, tmp_path):
    uploaded = tmp_path / "uploaded"
    uploaded.mkdir()
    (uploaded / ".user_created").write_text("1", encoding="utf-8")
    evolved = tmp_path / "evolved"
    evolved.mkdir()
    now = datetime(2026, 9, 12, 9, tzinfo=SHANGHAI).timestamp()
    monkeypatch.setattr(service, "skills_dir_for_profile", lambda _profile: tmp_path)
    monkeypatch.setattr(
        service,
        "list_installed",
        lambda _profile: {
            "skills": [
                {"name": "uploaded", "full_name": "uploaded", "can_delete": True},
                {"name": "system", "full_name": "system", "can_delete": False},
                {"name": "evolved", "full_name": "evolved", "can_delete": True},
            ]
        },
    )
    monkeypatch.setattr(service, "read_local_skill_mtime", lambda *_args: now)

    recent = service.list_recent_learned_skills("p1", date(2026, 9, 12))

    assert [item["name"] for item in recent] == ["evolved"]
    assert recent[0]["source"] == "memory_evolution"


def test_build_overview_uses_info_time_and_keeps_missing_time_compatible(monkeypatch, tmp_path):
    profile = {
        "name": "p1",
        "is_default": False,
        "info": {
            "display_name": "采购助理",
            "description": "协助采购",
            "logo": "data:image/png;base64,AA==",
            "time": "2026-09-01T10:20:30+08:00",
        },
    }
    monkeypatch.setattr(service, "resolve_profile", lambda _name: (profile, tmp_path))
    monkeypatch.setattr(service, "_collect_sessions", lambda *_args: (3, Counter()))
    monkeypatch.setattr(service, "_collect_cron_count", lambda _home: 2)
    monkeypatch.setattr(service, "list_recent_learned_skills", lambda *_args: [])
    monkeypatch.setattr(service, "list_raw_files", lambda _home: [])

    result = service.build_overview(
        "p1", now=datetime(2026, 9, 11, 12, 0, tzinfo=SHANGHAI)
    )

    assert result["profile"]["role_type"] == "position"
    assert result["profile"]["created_at"] == "2026-09-01T10:20:30+08:00"
    assert result["metrics"] == {
        "work_days": 10,
        "automatic_task_count": 2,
        "conversation_task_count": 3,
    }

    profile["info"].pop("time")
    result = service.build_overview(
        "p1", now=datetime(2026, 9, 11, 12, 0, tzinfo=SHANGHAI)
    )
    assert result["profile"]["created_at"] is None
    assert result["metrics"]["work_days"] == 0


def test_raw_files_are_fixed_and_only_existing_files_are_listed(tmp_path):
    (tmp_path / "memories").mkdir()
    (tmp_path / "SOUL.md").write_text("soul", encoding="utf-8")
    (tmp_path / "memories" / "USER.md").write_text("user", encoding="utf-8")
    (tmp_path / "SECRET.md").write_text("secret", encoding="utf-8")

    files = service.list_raw_files(tmp_path)

    assert [item["file_type"] for item in files] == ["soul", "user"]
    assert service.raw_file_path(tmp_path, "memory")[1] == tmp_path / "memories" / "MEMORY.md"

"""Tests for startup self-healing of missing .detail.json on user-created skills."""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from integration.skills import detail_bootstrap


SKILL_MD_TEMPLATE = """---
name: {name}
description: {desc}
---

# {name}

Skill body.
"""


def _setup_profiles(monkeypatch, tmp_path, profile_names):
    """Map profile names to isolated skills dirs under tmp_path."""
    hermes_homes = {}
    for name in profile_names:
        home = tmp_path / name
        (home / "skills").mkdir(parents=True, exist_ok=True)
        hermes_homes[name] = home

    def fake_get_hermes_home_for_profile(profile_name):
        return hermes_homes[profile_name]

    monkeypatch.setattr(
        "integration.skills.paths.get_hermes_home_for_profile",
        fake_get_hermes_home_for_profile,
    )
    monkeypatch.setattr(
        "api.profiles.list_profiles_api",
        lambda: [{"name": n} for n in profile_names],
    )
    monkeypatch.setattr(
        "integration.config.integration_enabled",
        lambda: True,
    )
    return {n: hermes_homes[n] / "skills" for n in profile_names}


def _make_skill(skills_dir: Path, name: str, *, markers=()):
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        SKILL_MD_TEMPLATE.format(name=name, desc=f"{name} skill"),
        encoding="utf-8",
    )
    for marker in markers:
        (skill_dir / marker).write_text("1", encoding="utf-8")
    return skill_dir


def _make_ai_meta_result(name: str):
    return {
        "name": name,
        "description": f"{name} description",
        "skillName": f"{name}-display",
        "displayDescription": f"{name} 显示描述",
        "detailJson": {"taskGoal": "demo", "taskDetails": []},
    }


def test_skill_with_no_detail_json_triggers_extraction(tmp_path, monkeypatch):
    skills_dirs = _setup_profiles(monkeypatch, tmp_path, ["default"])
    _make_skill(skills_dirs["default"], "my-skill", markers=[".user_created"])

    extract_calls = []

    def fake_extract(content, name="", description=""):
        extract_calls.append((content, name, description))
        return _make_ai_meta_result(name)

    monkeypatch.setattr("integration.skills.detail_bootstrap.extract_ai_meta", fake_extract)

    result = detail_bootstrap.bootstrap_missing_skill_details()

    assert result["scanned"] == 1
    assert result["missing"] == 1
    assert result["extracted"] == 1
    assert result["failed"] == []
    assert len(extract_calls) == 1
    detail_path = skills_dirs["default"] / "my-skill" / ".detail.json"
    assert detail_path.is_file()
    saved = json.loads(detail_path.read_text(encoding="utf-8"))
    assert saved["name"] == "my-skill"
    assert saved["display_name"] == "my-skill-display"
    assert saved["detail_json"] == {"taskGoal": "demo", "taskDetails": []}


def test_skill_with_incomplete_detail_json_triggers_extraction(tmp_path, monkeypatch):
    skills_dirs = _setup_profiles(monkeypatch, tmp_path, ["default"])
    skill_dir = _make_skill(skills_dirs["default"], "incomplete", markers=[".user_created"])
    # detail.json exists but missing detail_json field
    (skill_dir / ".detail.json").write_text(
        json.dumps({"name": "incomplete", "description": "desc"}),
        encoding="utf-8",
    )

    extract_calls = []

    def fake_extract(content, name="", description=""):
        extract_calls.append(name)
        return _make_ai_meta_result(name)

    monkeypatch.setattr("integration.skills.detail_bootstrap.extract_ai_meta", fake_extract)

    result = detail_bootstrap.bootstrap_missing_skill_details()

    assert result["missing"] == 1
    assert result["extracted"] == 1
    assert extract_calls == ["incomplete"]
    saved = json.loads((skill_dir / ".detail.json").read_text(encoding="utf-8"))
    assert "detail_json" in saved and saved["detail_json"]


def test_skill_with_complete_detail_json_is_skipped(tmp_path, monkeypatch):
    skills_dirs = _setup_profiles(monkeypatch, tmp_path, ["default"])
    skill_dir = _make_skill(skills_dirs["default"], "complete", markers=[".user_created"])
    (skill_dir / ".detail.json").write_text(
        json.dumps({
            "name": "complete",
            "description": "desc",
            "detail_json": {"taskGoal": "x"},
        }),
        encoding="utf-8",
    )

    extract_calls = MagicMock()
    monkeypatch.setattr(
        "integration.skills.detail_bootstrap.extract_ai_meta",
        lambda *a, **kw: extract_calls(*a, **kw),
    )

    result = detail_bootstrap.bootstrap_missing_skill_details()

    assert result["scanned"] == 1
    assert result["missing"] == 0
    assert result["extracted"] == 0
    extract_calls.assert_not_called()


def test_extraction_retries_3_times_then_gives_up(tmp_path, monkeypatch):
    skills_dirs = _setup_profiles(monkeypatch, tmp_path, ["default"])
    _make_skill(skills_dirs["default"], "failing", markers=[".user_created"])

    attempts = []

    def always_fail(content, name="", description=""):
        attempts.append(name)
        raise RuntimeError("upstream unavailable")

    monkeypatch.setattr("integration.skills.detail_bootstrap.extract_ai_meta", always_fail)
    monkeypatch.setattr("integration.skills.detail_bootstrap.time.sleep", lambda *_: None)
    monkeypatch.setattr("integration.skills.detail_bootstrap.random.random", lambda: 0.5)

    result = detail_bootstrap.bootstrap_missing_skill_details()

    assert len(attempts) == 3
    assert result["failed"] == ["failing"]
    assert result["extracted"] == 0
    assert not (skills_dirs["default"] / "failing" / ".detail.json").is_file()


def test_extraction_uses_exponential_backoff_with_jitter(tmp_path, monkeypatch):
    """Retry delays must follow exponential backoff (base * 2^(n-1)) with jitter,
    so concurrent instances don't synchronously retry against the same upstream.
    """
    skills_dirs = _setup_profiles(monkeypatch, tmp_path, ["default"])
    _make_skill(skills_dirs["default"], "backoff", markers=[".user_created"])

    def always_fail(content, name="", description=""):
        raise RuntimeError("transient")

    monkeypatch.setattr("integration.skills.detail_bootstrap.extract_ai_meta", always_fail)
    # Fix random.random() at 0.5 so jitter multiplier is exactly 1.0 (0.5 + 0.5)
    monkeypatch.setattr("integration.skills.detail_bootstrap.random.random", lambda: 0.5)

    sleep_calls = []
    monkeypatch.setattr(
        "integration.skills.detail_bootstrap.time.sleep",
        lambda d: sleep_calls.append(d),
    )

    detail_bootstrap.bootstrap_missing_skill_details()

    # 3 attempts -> 2 sleeps. base_delay=1.0, jitter=0.5+0.5=1.0
    # sleep 1: 1.0 * 2^0 * 1.0 = 1.0
    # sleep 2: 1.0 * 2^1 * 1.0 = 2.0
    assert len(sleep_calls) == 2
    assert sleep_calls[0] == 1.0
    assert sleep_calls[1] == 2.0


def test_extraction_succeeds_on_second_attempt(tmp_path, monkeypatch):
    skills_dirs = _setup_profiles(monkeypatch, tmp_path, ["default"])
    _make_skill(skills_dirs["default"], "retry-ok", markers=[".user_created"])

    attempts = []

    def fail_then_succeed(content, name="", description=""):
        attempts.append(name)
        if len(attempts) == 1:
            raise RuntimeError("transient")
        return _make_ai_meta_result(name)

    monkeypatch.setattr("integration.skills.detail_bootstrap.extract_ai_meta", fail_then_succeed)
    monkeypatch.setattr("integration.skills.detail_bootstrap.time.sleep", lambda *_: None)

    result = detail_bootstrap.bootstrap_missing_skill_details()

    assert len(attempts) == 2
    assert result["extracted"] == 1
    assert result["failed"] == []
    assert (skills_dirs["default"] / "retry-ok" / ".detail.json").is_file()


def test_detail_synced_to_all_profiles(tmp_path, monkeypatch):
    skills_dirs = _setup_profiles(monkeypatch, tmp_path, ["default", "work"])
    _make_skill(skills_dirs["default"], "shared", markers=[".user_created"])
    _make_skill(skills_dirs["work"], "shared", markers=[".user_created"])

    monkeypatch.setattr(
        "integration.skills.detail_bootstrap.extract_ai_meta",
        lambda content, name="", description="": _make_ai_meta_result(name),
    )

    result = detail_bootstrap.bootstrap_missing_skill_details()

    assert result["scanned"] == 1  # deduplicated by logical name
    assert result["extracted"] == 1

    default_detail = json.loads(
        (skills_dirs["default"] / "shared" / ".detail.json").read_text(encoding="utf-8")
    )
    work_detail = json.loads(
        (skills_dirs["work"] / "shared" / ".detail.json").read_text(encoding="utf-8")
    )
    assert default_detail == work_detail
    assert default_detail["name"] == "shared"


def test_hub_installed_skill_is_skipped(tmp_path, monkeypatch):
    skills_dirs = _setup_profiles(monkeypatch, tmp_path, ["default"])
    _make_skill(skills_dirs["default"], "market", markers=[".hub_installed"])

    extract_calls = MagicMock()
    monkeypatch.setattr(
        "integration.skills.detail_bootstrap.extract_ai_meta",
        lambda *a, **kw: extract_calls(*a, **kw),
    )

    result = detail_bootstrap.bootstrap_missing_skill_details()

    assert result["scanned"] == 0
    assert result["missing"] == 0
    extract_calls.assert_not_called()
    assert not (skills_dirs["default"] / "market" / ".detail.json").is_file()


def test_session_created_skill_without_marker_is_included(tmp_path, monkeypatch):
    skills_dirs = _setup_profiles(monkeypatch, tmp_path, ["default"])
    # No .user_created, no .hub_installed - conversation-created
    _make_skill(skills_dirs["default"], "session-skill", markers=[])

    extract_calls = []

    def fake_extract(content, name="", description=""):
        extract_calls.append(name)
        return _make_ai_meta_result(name)

    monkeypatch.setattr("integration.skills.detail_bootstrap.extract_ai_meta", fake_extract)

    result = detail_bootstrap.bootstrap_missing_skill_details()

    assert result["scanned"] == 1
    assert result["missing"] == 1
    assert result["extracted"] == 1
    assert extract_calls == ["session-skill"]
    assert (skills_dirs["default"] / "session-skill" / ".detail.json").is_file()


def test_integration_disabled_returns_zeros(tmp_path, monkeypatch):
    monkeypatch.setattr("integration.config.integration_enabled", lambda: False)

    # These should never be called if integration is disabled
    list_calls = MagicMock()
    monkeypatch.setattr("api.profiles.list_profiles_api", lambda: list_calls())

    result = detail_bootstrap.bootstrap_missing_skill_details()

    assert result == {"scanned": 0, "missing": 0, "extracted": 0, "synced": 0, "failed": []}
    list_calls.assert_not_called()


def test_category_nested_skill_is_scanned(tmp_path, monkeypatch):
    """Skills nested under a category directory (skills/<cat>/<skill>/SKILL.md)
    must be found by the recursive walker, matching _scan_custom_skill_dicts.
    """
    skills_dirs = _setup_profiles(monkeypatch, tmp_path, ["default"])
    # Create a category-nested skill: skills/data-analysis/my-skill/SKILL.md
    skill_dir = skills_dirs["default"] / "data-analysis" / "my-skill"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        SKILL_MD_TEMPLATE.format(name="my-skill", desc="nested skill"),
        encoding="utf-8",
    )
    (skill_dir / ".user_created").write_text("1", encoding="utf-8")

    extract_calls = []

    def fake_extract(content, name="", description=""):
        extract_calls.append(name)
        return _make_ai_meta_result(name)

    monkeypatch.setattr("integration.skills.detail_bootstrap.extract_ai_meta", fake_extract)

    result = detail_bootstrap.bootstrap_missing_skill_details()

    assert result["scanned"] == 1
    assert result["missing"] == 1
    assert result["extracted"] == 1
    assert extract_calls == ["my-skill"]
    detail_path = skill_dir / ".detail.json"
    assert detail_path.is_file()
    saved = json.loads(detail_path.read_text(encoding="utf-8"))
    assert saved["name"] == "my-skill"


def test_save_skipped_when_detail_becomes_complete_during_extraction(tmp_path, monkeypatch):
    """If .detail.json is filled by a concurrent caller (e.g. frontend's /detail
    endpoint) during the extraction window, bootstrap must NOT overwrite it.
    """
    skills_dirs = _setup_profiles(monkeypatch, tmp_path, ["default"])
    skill_dir = _make_skill(skills_dirs["default"], "racy", markers=[".user_created"])

    frontend_payload = {
        "name": "racy",
        "description": "frontend-saved desc",
        "display_name": "racy-frontend-display",
        "display_description": "前端保存的显示名",
        "detail_json": {"taskGoal": "frontend-goal", "taskDetails": ["a"]},
    }

    def extract_that_simulates_concurrent_save(content, name="", description=""):
        # Simulate the frontend saving .detail.json during the extraction window
        (skill_dir / ".detail.json").write_text(
            json.dumps(frontend_payload, ensure_ascii=False), encoding="utf-8"
        )
        return _make_ai_meta_result(name)

    monkeypatch.setattr(
        "integration.skills.detail_bootstrap.extract_ai_meta",
        extract_that_simulates_concurrent_save,
    )

    result = detail_bootstrap.bootstrap_missing_skill_details()

    assert result["extracted"] == 1
    assert result["failed"] == []
    # The file must still hold the frontend's content, not bootstrap's payload
    saved = json.loads((skill_dir / ".detail.json").read_text(encoding="utf-8"))
    assert saved == frontend_payload
    assert saved["display_name"] == "racy-frontend-display"
    assert saved["detail_json"] == {"taskGoal": "frontend-goal", "taskDetails": ["a"]}


def test_description_passed_from_skill_md_so_result_is_stable(tmp_path, monkeypatch):
    """Description must be read from SKILL.md frontmatter and passed to
    extract_ai_meta, so the saved .detail.json has a non-empty description
    and won't be re-extracted on every restart.
    """
    skills_dirs = _setup_profiles(monkeypatch, tmp_path, ["default"])
    skill_dir = skills_dirs["default"] / "my-skill"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: my-skill\ndescription: Throwaway HTML mockups.\n---\n# my-skill\nbody",
        encoding="utf-8",
    )
    (skill_dir / ".user_created").write_text("1", encoding="utf-8")

    captured = {}

    def fake_extract(content, name="", description=""):
        captured["name"] = name
        captured["description"] = description
        # Mirror real extract_ai_meta: pass name/description through
        return {
            "name": name or None,
            "description": description or None,
            "skillName": f"{name}-display",
            "displayDescription": "显示描述",
            "detailJson": {"taskGoal": "demo", "taskDetails": []},
        }

    monkeypatch.setattr("integration.skills.detail_bootstrap.extract_ai_meta", fake_extract)

    # First run: extracts and saves
    result = detail_bootstrap.bootstrap_missing_skill_details()
    assert result["extracted"] == 1
    assert captured["description"] == "Throwaway HTML mockups."

    saved_path = skill_dir / ".detail.json"
    saved = json.loads(saved_path.read_text(encoding="utf-8"))
    assert saved["description"] == "Throwaway HTML mockups."

    # Second run: .detail.json is now complete, must NOT re-extract
    extract_calls_after = []

    def fail_if_called(content, name="", description=""):
        extract_calls_after.append(name)
        return _make_ai_meta_result(name)

    monkeypatch.setattr("integration.skills.detail_bootstrap.extract_ai_meta", fail_if_called)
    result2 = detail_bootstrap.bootstrap_missing_skill_details()
    assert result2["missing"] == 0
    assert result2["extracted"] == 0
    assert extract_calls_after == []


def test_sync_from_complete_profile_skips_upstream_call(tmp_path, monkeypatch):
    """When one profile already has a complete .detail.json and another doesn't,
    bootstrap must copy from the complete one WITHOUT calling extract_ai_meta.
    """
    skills_dirs = _setup_profiles(monkeypatch, tmp_path, ["default", "work"])
    # default: complete .detail.json
    default_dir = _make_skill(skills_dirs["default"], "shared", markers=[".user_created"])
    complete_payload = {
        "name": "shared",
        "description": "already complete",
        "display_name": "shared-display",
        "display_description": "已完整",
        "detail_json": {"taskGoal": "x", "taskDetails": []},
    }
    (default_dir / ".detail.json").write_text(
        json.dumps(complete_payload, ensure_ascii=False), encoding="utf-8"
    )
    # work: no .detail.json (incomplete)
    _make_skill(skills_dirs["work"], "shared", markers=[".user_created"])

    extract_calls = []

    def fail_if_called(content, name="", description=""):
        extract_calls.append(name)
        return _make_ai_meta_result(name)

    monkeypatch.setattr("integration.skills.detail_bootstrap.extract_ai_meta", fail_if_called)

    result = detail_bootstrap.bootstrap_missing_skill_details()

    assert result["scanned"] == 1
    assert result["missing"] == 1
    assert result["synced"] == 1
    assert result["extracted"] == 0
    assert result["failed"] == []
    assert extract_calls == []  # no upstream call

    # work profile now has the same .detail.json as default
    work_detail = json.loads(
        (skills_dirs["work"] / "shared" / ".detail.json").read_text(encoding="utf-8")
    )
    assert work_detail == complete_payload

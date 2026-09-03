"""Tests for integration.skills.version_check."""

from __future__ import annotations

from unittest.mock import patch

from integration.skills import version_check
from integration.skills import version_store


def _make_db(tmp_path):
    return tmp_path / "test.db"


def _seed_installed(db):
    """Minimal DB state for a check to operate on."""
    version_store.record_install(
        "demo-skill", local_version="1.0.0", display_name="Demo",
        profile="default", dir_name="tools/demo", db_path=db,
    )
    version_store.record_install(
        "other-skill", local_version="2.0.0", display_name="Other",
        profile="default", dir_name="tools/other", db_path=db,
    )


# ---------------------------------------------------------------------------
# Empty / no skills
# ---------------------------------------------------------------------------

def test_check_returns_zero_when_no_installed(tmp_path, monkeypatch):
    db = _make_db(tmp_path)
    monkeypatch.setattr(
        "integration.skills.skillhub._hub_installed_index_all_profiles", lambda: {}
    )
    result = version_check.check_updates_for_installed_skills(db_path=db)
    assert result == {"checked": 0, "upgradable": 0, "auto_updated": 0}


# ---------------------------------------------------------------------------
# Batch hit
# ---------------------------------------------------------------------------

def test_check_writes_upstream_from_batch(tmp_path, monkeypatch):
    db = _make_db(tmp_path)
    _seed_installed(db)
    monkeypatch.setattr(
        "integration.skills.skillhub._hub_installed_index_all_profiles",
        lambda: {"demo-skill": ("default", "tools/demo"), "other-skill": ("default", "tools/other")},
    )
    batch_return = {
        "items": [
            {"name": "demo-skill", "version": "1.2.0", "change_logs": [{"type": "新增", "changeLog": "新功能"}], "published_at": "2026-08-20 09:00:00"},
            {"name": "other-skill", "version": "2.0.0", "change_logs": None, "published_at": ""},
        ],
        "missing": [],
    }
    monkeypatch.setattr(
        "integration.skills.skillhub.fetch_versions_batch", lambda names: batch_return
    )
    monkeypatch.setattr("api.config.load_settings", lambda: {})

    result = version_check.check_updates_for_installed_skills(db_path=db)
    assert result["checked"] == 2
    assert result["upgradable"] == 1  # demo-skill only

    row = version_store.get("demo-skill", db_path=db)
    assert row["upstream_version"] == "1.2.0"
    assert row["upstream_change_logs"][0]["changeLog"] == "新功能"


# ---------------------------------------------------------------------------
# Missing skills marked unreachable
# ---------------------------------------------------------------------------

def test_check_marks_missing_unreachable(tmp_path, monkeypatch):
    db = _make_db(tmp_path)
    _seed_installed(db)
    monkeypatch.setattr(
        "integration.skills.skillhub._hub_installed_index_all_profiles",
        lambda: {"demo-skill": ("default", "tools/demo"), "other-skill": ("default", "tools/other")},
    )
    batch_return = {
        "items": [
            {"name": "demo-skill", "version": "1.2.0", "changeLogs": [], "publishedAt": ""},
        ],
        "missing": ["other-skill"],
    }
    monkeypatch.setattr(
        "integration.skills.skillhub.fetch_versions_batch", lambda names: batch_return
    )
    monkeypatch.setattr("api.config.load_settings", lambda: {})

    version_check.check_updates_for_installed_skills(db_path=db)
    row = version_store.get("other-skill", db_path=db)
    assert row["upstream_unreachable"] == 1


# ---------------------------------------------------------------------------
# Network failure — all marked unreachable, no exception
# ---------------------------------------------------------------------------

def test_check_network_failure_marks_all_unreachable(tmp_path, monkeypatch):
    db = _make_db(tmp_path)
    _seed_installed(db)
    monkeypatch.setattr(
        "integration.skills.skillhub._hub_installed_index_all_profiles",
        lambda: {"demo-skill": ("default", "tools/demo"), "other-skill": ("default", "tools/other")},
    )
    monkeypatch.setattr(
        "integration.skills.skillhub.fetch_versions_batch",
        lambda names: (_ for _ in ()).throw(RuntimeError("network down")),
    )

    result = version_check.check_updates_for_installed_skills(db_path=db)
    assert result["checked"] == 2
    assert result["upgradable"] == 0
    assert version_store.get("demo-skill", db_path=db)["upstream_unreachable"] == 1
    assert version_store.get("other-skill", db_path=db)["upstream_unreachable"] == 1


# ---------------------------------------------------------------------------
# Auto-update ON
# ---------------------------------------------------------------------------

def test_check_auto_update_calls_upgrade(tmp_path, monkeypatch):
    db = _make_db(tmp_path)
    _seed_installed(db)
    monkeypatch.setattr(
        "integration.skills.skillhub._hub_installed_index_all_profiles",
        lambda: {"demo-skill": ("default", "tools/demo"), "other-skill": ("default", "tools/other")},
    )
    batch_return = {
        "items": [
            {"name": "demo-skill", "version": "1.2.0", "changeLogs": [], "publishedAt": ""},
            {"name": "other-skill", "version": "2.1.0", "change_logs": [], "published_at": ""},
        ],
        "missing": [],
    }
    monkeypatch.setattr(
        "integration.skills.skillhub.fetch_versions_batch", lambda names: batch_return
    )
    monkeypatch.setattr("api.config.load_settings", lambda: {"skills_auto_update": True})

    upgrade_calls = []

    def fake_upgrade(name, action="upgrade"):
        upgrade_calls.append((name, action))
        # Simulate successful upgrade: bump local_version
        version_store.record_upgrade(
            name, new_version="9.9.9", action=action, db_path=db,
        )
        return {"ok": True, "name": name, "version": "9.9.9"}

    monkeypatch.setattr("integration.skills.skillhub.upgrade_skill", fake_upgrade)

    result = version_check.check_updates_for_installed_skills(db_path=db)
    assert result["auto_updated"] == 2
    assert len(upgrade_calls) == 2
    # After auto-upgrade, upgradable should be 0
    assert result["upgradable"] == 0


# ---------------------------------------------------------------------------
# Auto-update OFF
# ---------------------------------------------------------------------------

def test_check_auto_update_off_skips_upgrade(tmp_path, monkeypatch):
    db = _make_db(tmp_path)
    _seed_installed(db)
    monkeypatch.setattr(
        "integration.skills.skillhub._hub_installed_index_all_profiles",
        lambda: {"demo-skill": ("default", "tools/demo"), "other-skill": ("default", "tools/other")},
    )
    batch_return = {
        "items": [
            {"name": "demo-skill", "version": "1.2.0", "changeLogs": [], "publishedAt": ""},
            {"name": "other-skill", "version": "2.1.0", "change_logs": [], "published_at": ""},
        ],
        "missing": [],
    }
    monkeypatch.setattr(
        "integration.skills.skillhub.fetch_versions_batch", lambda names: batch_return
    )
    monkeypatch.setattr("api.config.load_settings", lambda: {"skills_auto_update": False})

    upgrade_calls = []
    monkeypatch.setattr(
        "integration.skills.skillhub.upgrade_skill",
        lambda name, action="upgrade": upgrade_calls.append(name),
    )

    result = version_check.check_updates_for_installed_skills(db_path=db)
    assert result["auto_updated"] == 0
    assert upgrade_calls == []
    assert result["upgradable"] == 2


# ---------------------------------------------------------------------------
# Auto-update partial failure
# ---------------------------------------------------------------------------

def test_check_auto_update_one_failure_continues_others(tmp_path, monkeypatch):
    db = _make_db(tmp_path)
    _seed_installed(db)
    monkeypatch.setattr(
        "integration.skills.skillhub._hub_installed_index_all_profiles",
        lambda: {"demo-skill": ("default", "tools/demo"), "other-skill": ("default", "tools/other")},
    )
    batch_return = {
        "items": [
            {"name": "demo-skill", "version": "1.2.0", "change_logs": [], "published_at": ""},
            {"name": "other-skill", "version": "2.1.0", "change_logs": [], "published_at": ""},
        ],
        "missing": [],
    }
    monkeypatch.setattr(
        "integration.skills.skillhub.fetch_versions_batch", lambda names: batch_return
    )
    monkeypatch.setattr("api.config.load_settings", lambda: {"skills_auto_update": True})

    def fake_upgrade(name, action="upgrade"):
        if name == "demo-skill":
            raise RuntimeError("download failed")
        version_store.record_upgrade(name, new_version="2.1.0", action=action, db_path=db)
        return {"ok": True, "name": name, "version": "2.1.0"}

    monkeypatch.setattr("integration.skills.skillhub.upgrade_skill", fake_upgrade)

    result = version_check.check_updates_for_installed_skills(db_path=db)
    assert result["auto_updated"] == 1  # only other-skill succeeded

"""Tests for integration.skills.version_store (DB layer + normalize)."""

from __future__ import annotations

from integration.skills import version_store


# ---------------------------------------------------------------------------
# normalize_change_logs
# ---------------------------------------------------------------------------

def test_normalize_valid():
    raw = [
        {"type": "新增", "changeLog": "新增批量处理模式"},
        {"type": "修复", "changeLog": "修复崩溃"},
    ]
    assert version_store.normalize_change_logs(raw) == raw


def test_normalize_snake_case_key():
    """Upstream fields are mapped to snake_case by _map_upstream_fields."""
    raw = [
        {"type": "新增", "change_log": "新增批量处理模式"},
        {"type": "修复", "change_log": "修复崩溃"},
    ]
    result = version_store.normalize_change_logs(raw)
    assert len(result) == 2
    assert result[0]["type"] == "新增"
    assert result[0]["changeLog"] == "新增批量处理模式"


def test_normalize_none_returns_empty():
    assert version_store.normalize_change_logs(None) == []
    assert version_store.normalize_change_logs("bad") == []


def test_normalize_invalid_type_dropped():
    raw = [
        {"type": "INVALID", "changeLog": "test"},
        {"type": "新增", "changeLog": "ok"},
    ]
    result = version_store.normalize_change_logs(raw)
    assert len(result) == 1
    assert result[0]["type"] == "新增"


def test_normalize_long_changelog_truncated():
    raw = [{"type": "新增", "changeLog": "x" * 300}]
    result = version_store.normalize_change_logs(raw)
    assert len(result[0]["changeLog"]) == 200


def test_normalize_max_20_entries():
    raw = [{"type": "修复", "changeLog": f"fix-{i}"} for i in range(25)]
    result = version_store.normalize_change_logs(raw)
    assert len(result) == 20


def test_normalize_empty_changelog_dropped():
    raw = [{"type": "新增", "changeLog": ""}]
    assert version_store.normalize_change_logs(raw) == []


def test_normalize_non_dict_items_dropped():
    raw = [42, {"type": "新增", "changeLog": "ok"}, "bad"]
    result = version_store.normalize_change_logs(raw)
    assert len(result) == 1


# ---------------------------------------------------------------------------
# Tables idempotent
# ---------------------------------------------------------------------------

def test_tables_created_twice(tmp_path):
    db = tmp_path / "test.db"
    version_store._connect(db)
    version_store._connect(db)  # should not raise


# ---------------------------------------------------------------------------
# record_install
# ---------------------------------------------------------------------------

def test_record_install_inserts_status(tmp_path):
    db = tmp_path / "test.db"
    version_store.record_install(
        "demo", display_name="Demo", category="tools",
        local_version="1.0.0",
        change_logs=[{"type": "新增", "changeLog": "初始发布"}],
        published_at="2026-08-01 10:00:00",
        profile="default", dir_name="tools/demo", db_path=db,
    )
    row = version_store.get("demo", db_path=db)
    assert row is not None
    assert row["local_version"] == "1.0.0"
    assert row["display_name"] == "Demo"
    profiles = row["installed_profiles"]
    assert len(profiles) == 1
    assert profiles[0]["profile"] == "default"


def test_record_install_appends_profile(tmp_path):
    db = tmp_path / "test.db"
    version_store.record_install(
        "demo", local_version="1.0.0",
        profile="default", dir_name="tools/demo", db_path=db,
    )
    version_store.record_install(
        "demo", local_version="1.0.0",
        profile="work", dir_name="tools/demo", db_path=db,
    )
    row = version_store.get("demo", db_path=db)
    profiles = row["installed_profiles"]
    assert len(profiles) == 2
    names = [p["profile"] for p in profiles]
    assert "default" in names and "work" in names


def test_record_install_no_duplicate_profile(tmp_path):
    db = tmp_path / "test.db"
    version_store.record_install(
        "demo", local_version="1.0.0",
        profile="default", dir_name="tools/demo", db_path=db,
    )
    version_store.record_install(
        "demo", local_version="1.0.0",
        profile="default", dir_name="tools/demo", db_path=db,
    )
    row = version_store.get("demo", db_path=db)
    assert len(row["installed_profiles"]) == 1


# ---------------------------------------------------------------------------
# record_upgrade
# ---------------------------------------------------------------------------

def test_record_upgrade_updates_local_version(tmp_path):
    db = tmp_path / "test.db"
    version_store.record_install(
        "demo", local_version="1.0.0",
        profile="default", dir_name="tools/demo", db_path=db,
    )
    version_store.record_upgrade(
        "demo", new_version="1.1.0",
        change_logs=[{"type": "优化", "changeLog": "性能提升"}],
        action="upgrade", db_path=db,
    )
    row = version_store.get("demo", db_path=db)
    assert row["local_version"] == "1.1.0"
    assert row["upgraded_at"] is not None


# ---------------------------------------------------------------------------
# refresh_upstream / mark_upstream_unreachable
# ---------------------------------------------------------------------------

def test_refresh_upstream_writes_version_and_clears_unreachable(tmp_path):
    db = tmp_path / "test.db"
    version_store.record_install(
        "demo", local_version="1.0.0",
        profile="default", dir_name="tools/demo", db_path=db,
    )
    version_store.mark_upstream_unreachable("demo", db_path=db)
    assert version_store.get("demo", db_path=db)["upstream_unreachable"] == 1

    version_store.refresh_upstream(
        "demo", upstream_version="1.2.0",
        change_logs=[{"type": "新增", "changeLog": "新功能"}],
        db_path=db,
    )
    row = version_store.get("demo", db_path=db)
    assert row["upstream_version"] == "1.2.0"
    assert row["upstream_unreachable"] == 0
    assert row["upstream_change_logs"][0]["changeLog"] == "新功能"


def test_mark_upstream_unreachable_preserves_old_version(tmp_path):
    db = tmp_path / "test.db"
    version_store.record_install(
        "demo", local_version="1.0.0", db_path=db,
        profile="default", dir_name="tools/demo",
    )
    version_store.refresh_upstream(
        "demo", upstream_version="1.1.0", db_path=db,
    )
    version_store.mark_upstream_unreachable("demo", db_path=db)
    row = version_store.get("demo", db_path=db)
    assert row["upstream_unreachable"] == 1
    assert row["upstream_version"] == "1.1.0"  # preserved


# ---------------------------------------------------------------------------
# list_upgradable
# ---------------------------------------------------------------------------

def test_list_upgradable_filters(tmp_path):
    db = tmp_path / "test.db"
    version_store.record_install(
        "old", local_version="1.0.0",
        profile="default", dir_name="tools/old", db_path=db,
    )
    version_store.refresh_upstream("old", upstream_version="1.1.0", db_path=db)

    version_store.record_install(
        "current", local_version="1.0.0",
        profile="default", dir_name="tools/current", db_path=db,
    )
    version_store.refresh_upstream("current", upstream_version="1.0.0", db_path=db)

    result = version_store.list_upgradable(db_path=db)
    names = [r["catalog_name"] for r in result]
    assert "old" in names
    assert "current" not in names


# ---------------------------------------------------------------------------
# remove
# ---------------------------------------------------------------------------

def test_remove_cleans_status_row(tmp_path):
    db = tmp_path / "test.db"
    version_store.record_install(
        "demo", local_version="1.0.0",
        profile="default", dir_name="tools/demo", db_path=db,
    )
    version_store.refresh_upstream("demo", upstream_version="1.1.0", db_path=db)
    version_store.remove("demo", db_path=db)

    assert version_store.get("demo", db_path=db) is None


def test_remove_nonexistent_no_error(tmp_path):
    db = tmp_path / "test.db"
    version_store.remove("no-such-skill", db_path=db)  # should not raise


# ---------------------------------------------------------------------------
# list_all
# ---------------------------------------------------------------------------

def test_list_all_empty(tmp_path):
    db = tmp_path / "test.db"
    assert version_store.list_all(db_path=db) == []


def test_list_all_returns_inserted(tmp_path):
    db = tmp_path / "test.db"
    version_store.record_install(
        "a", local_version="1.0.0", db_path=db, profile="default", dir_name="a",
    )
    version_store.record_install(
        "b", local_version="2.0.0", db_path=db, profile="default", dir_name="b",
    )
    result = version_store.list_all(db_path=db)
    names = [r["catalog_name"] for r in result]
    assert names == ["a", "b"]

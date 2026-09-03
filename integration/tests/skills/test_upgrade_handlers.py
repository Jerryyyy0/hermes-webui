"""Tests for version-update handler endpoints."""

from __future__ import annotations

from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

from integration.skills import version_store


def _make_db(tmp_path):
    return tmp_path / "test.db"


def _parsed(path, qs=""):
    return urlparse(path + ("?" + qs if qs else ""))


# ---------------------------------------------------------------------------
# GET /api/skillhub/updates
# ---------------------------------------------------------------------------

def test_updates_endpoint_returns_items(tmp_path):
    db = _make_db(tmp_path)
    version_store.record_install(
        "demo", local_version="1.0.0", profile="default", dir_name="tools/demo", db_path=db,
    )
    version_store.refresh_upstream("demo", upstream_version="1.2.0", db_path=db)

    handler = MagicMock()
    with patch("integration.skills.handlers.skillhub_enabled", return_value=True):
        with patch("integration.skills.handlers._DB_PATH", db):
            with patch("api.config.load_settings", return_value={}):
                from integration.skills.handlers import try_handle_get
                result = try_handle_get(handler, _parsed("/api/skillhub/updates"))
    assert result is True


# ---------------------------------------------------------------------------
# GET /api/skillhub/updates/summary
# ---------------------------------------------------------------------------

def test_summary_endpoint_returns_count(tmp_path):
    db = _make_db(tmp_path)
    version_store.record_install(
        "demo", local_version="1.0.0", profile="default", dir_name="tools/demo", db_path=db,
    )
    version_store.refresh_upstream("demo", upstream_version="1.2.0", db_path=db)

    handler = MagicMock()
    with patch("integration.skills.handlers.skillhub_enabled", return_value=True):
        with patch("integration.skills.handlers._DB_PATH", db):
            with patch("api.config.load_settings", return_value={}):
                from integration.skills.handlers import try_handle_get
                result = try_handle_get(handler, _parsed("/api/skillhub/updates/summary"))
    assert result is True


# ---------------------------------------------------------------------------
# POST /api/skillhub/upgrade
# ---------------------------------------------------------------------------

def test_upgrade_missing_name():
    handler = MagicMock()
    with patch("integration.skills.handlers.skillhub_enabled", return_value=True):
        from integration.skills.handlers import try_handle_post
        result = try_handle_post(handler, _parsed("/api/skillhub/upgrade"), {})
    assert result is True


def test_upgrade_not_installed(tmp_path):
    db = _make_db(tmp_path)
    handler = MagicMock()
    with patch("integration.skills.handlers.skillhub_enabled", return_value=True):
        with patch("integration.skills.handlers._DB_PATH", db):
            from integration.skills.handlers import try_handle_post
            result = try_handle_post(
                handler, _parsed("/api/skillhub/upgrade"), {"name": "no-such"}
            )
    assert result is True


def test_upgrade_success(tmp_path):
    db = _make_db(tmp_path)
    version_store.record_install(
        "demo", local_version="1.0.0", profile="default", dir_name="tools/demo", db_path=db,
    )

    def fake_upgrade(name, action="upgrade"):
        version_store.record_upgrade(name, new_version="1.2.0", action=action, db_path=db)
        return {"ok": True, "name": name, "version": "1.2.0", "results": []}

    handler = MagicMock()
    with patch("integration.skills.handlers.skillhub_enabled", return_value=True):
        with patch("integration.skills.handlers._DB_PATH", db):
            with patch("integration.skills.skillhub.upgrade_skill", side_effect=fake_upgrade):
                from integration.skills.handlers import try_handle_post
                result = try_handle_post(
                    handler, _parsed("/api/skillhub/upgrade"), {"name": "demo"}
                )
    assert result is True


# ---------------------------------------------------------------------------
# GET /api/skillhub/skill-versions
# ---------------------------------------------------------------------------

def test_skill_versions_returns_upstream_data(tmp_path):
    db = _make_db(tmp_path)
    upstream_versions = [
        {"version": "1.1.0", "change_logs": [{"type": "优化", "changeLog": "提升"}], "published_at": "2026-08-15 10:00:00"},
        {"version": "1.0.0", "change_logs": [{"type": "新增", "changeLog": "初始"}], "published_at": "2026-08-01 10:00:00"},
    ]

    handler = MagicMock()
    with patch("integration.skills.handlers.skillhub_enabled", return_value=True):
        with patch("integration.skills.handlers._DB_PATH", db):
            with patch(
                "integration.skills.skillhub.fetch_version_history",
                return_value=upstream_versions,
            ):
                from integration.skills.handlers import try_handle_get
                result = try_handle_get(
                    handler, _parsed("/api/skillhub/skill-versions", "name=demo")
                )
    assert result is True


def test_skill_versions_missing_name():
    handler = MagicMock()
    with patch("integration.skills.handlers.skillhub_enabled", return_value=True):
        from integration.skills.handlers import try_handle_get
        result = try_handle_get(handler, _parsed("/api/skillhub/skill-versions"))
    assert result is True


def test_skill_versions_upstream_unavailable_returns_empty(tmp_path):
    """When upstream is unreachable, versions should be empty (no local cache)."""
    db = _make_db(tmp_path)

    handler = MagicMock()
    with patch("integration.skills.handlers.skillhub_enabled", return_value=True):
        with patch("integration.skills.handlers._DB_PATH", db):
            with patch(
                "integration.skills.skillhub.fetch_version_history",
                side_effect=RuntimeError("upstream down"),
            ):
                from integration.skills.handlers import try_handle_get
                result = try_handle_get(
                    handler, _parsed("/api/skillhub/skill-versions", "name=demo")
                )
    assert result is True


# ---------------------------------------------------------------------------
# skillhub_enabled()=False returns False
# ---------------------------------------------------------------------------

def test_updates_not_handled_when_disabled():
    handler = MagicMock()
    with patch("integration.skills.handlers.skillhub_enabled", return_value=False):
        with patch("integration.skills.handlers.integration_enabled", return_value=True):
            from integration.skills.handlers import try_handle_get
            result = try_handle_get(handler, _parsed("/api/skillhub/updates"))
    assert result is False


def test_upgrade_not_handled_when_disabled():
    handler = MagicMock()
    with patch("integration.skills.handlers.skillhub_enabled", return_value=False):
        with patch("integration.skills.handlers.integration_enabled", return_value=True):
            from integration.skills.handlers import try_handle_post
            result = try_handle_post(
                handler, _parsed("/api/skillhub/upgrade"), {"name": "demo"}
            )
    assert result is False

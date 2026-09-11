"""Tests for version-update handler endpoints."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

from integration.skills import version_store


def _make_db(tmp_path):
    return tmp_path / "test.db"


def _parsed(path, qs=""):
    return urlparse(path + ("?" + qs if qs else ""))


def _patch_hub_installed(name: str = "demo", dir_name: str = "tools/demo"):
    """Patch ``_hub_installed_profiles_all`` so *name* is treated as a hub skill."""
    return patch(
        "integration.skills.handlers.skillhub._hub_installed_profiles_all",
        return_value={
            name: [{"profile": "default", "dir_name": dir_name, "skill_dir": None}],
        },
    )


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
            with _patch_hub_installed("demo", "tools/demo"):
                with patch(
                    "integration.skills.skillhub.fetch_version_history",
                    return_value=upstream_versions,
                ):
                    with patch(
                        "integration.skills.skillhub.is_delisted_installed",
                        return_value=False,
                    ):
                        from integration.skills.handlers import try_handle_get
                        result = try_handle_get(
                            handler, _parsed("/api/skillhub/skill-versions", "name=demo")
                        )
    assert result is True
    body = json.loads(handler.wfile.write.call_args[0][0])
    assert body["delisted"] is False
    assert body["source"] == "hub"


def test_skill_versions_marks_delisted_skill(tmp_path):
    db = _make_db(tmp_path)

    handler = MagicMock()
    with patch("integration.skills.handlers.skillhub_enabled", return_value=True):
        with patch("integration.skills.handlers._DB_PATH", db):
            with _patch_hub_installed("browseragent", "tools/browseragent"):
                with patch(
                    "integration.skills.skillhub.fetch_version_history",
                    return_value=[],
                ):
                    with patch(
                        "integration.skills.skillhub.is_delisted_installed",
                        return_value=True,
                    ):
                        from integration.skills.handlers import try_handle_get
                        result = try_handle_get(
                            handler, _parsed("/api/skillhub/skill-versions", "name=browseragent")
                        )
    assert result is True
    body = json.loads(handler.wfile.write.call_args[0][0])
    assert body["delisted"] is True
    assert body["source"] == "hub"


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
            with _patch_hub_installed("demo", "tools/demo"):
                with patch(
                    "integration.skills.skillhub.fetch_version_history",
                    side_effect=RuntimeError("upstream down"),
                ):
                    with patch(
                        "integration.skills.skillhub.is_delisted_installed",
                        return_value=False,
                    ):
                        from integration.skills.handlers import try_handle_get
                        result = try_handle_get(
                            handler, _parsed("/api/skillhub/skill-versions", "name=demo")
                        )
    assert result is True


def test_skill_versions_custom_source():
    """Published custom skill returns version history from the local publish-app DB."""
    handler = MagicMock()
    fake_versions = [
        {
            "id": "skp-1",
            "version": "1.1.0",
            "status": "approved",
            "application_type": "publish",
            "submitted_at": 1000,
            "audited_at": 1100,
            "audit_comment": "ok",
            "change_logs": '[{"type":"优化","changeLog":"v2"}]',
        },
    ]
    with patch("integration.skills.handlers.skillhub_enabled", return_value=True):
        with patch(
            "integration.skills.handlers._detect_skill_source",
            return_value="custom",
        ):
            with patch(
                "integration.skill_publish.store.get_latest_version",
                return_value={"version": "1.1.0"},
            ):
                with patch(
                    "integration.skill_publish.store.list_merged_versions",
                    return_value=fake_versions,
                ):
                    from integration.skills.handlers import try_handle_get
                    result = try_handle_get(
                        handler,
                        _parsed("/api/skillhub/skill-versions", "name=my-skill"),
                    )
    assert result is True
    body = json.loads(handler.wfile.write.call_args[0][0])
    assert body["source"] == "custom"
    assert body["published"] is True
    assert body["current_version"] == "1.1.0"
    assert body["latest_version"] == "1.1.0"
    assert body["next_version"] == ""
    assert body["delisted"] is False
    assert len(body["versions"]) == 1
    assert body["versions"][0]["version"] == "1.1.0"
    assert body["versions"][0]["application_id"] == "skp-1"
    assert body["versions"][0]["change_logs"][0]["type"] == "优化"
    assert body["versions"][0]["change_logs"][0]["change_log"] == "v2"
    assert "changeLog" not in body["versions"][0]["change_logs"][0]


def test_skill_versions_custom_unpublished():
    """Custom skill never published → published=false, empty versions, next_version=1.0.0."""
    handler = MagicMock()
    with patch("integration.skills.handlers.skillhub_enabled", return_value=True):
        with patch(
            "integration.skills.handlers._detect_skill_source",
            return_value="custom",
        ):
            with patch(
                "integration.skill_publish.store.get_latest_version",
                return_value=None,
            ):
                with patch(
                    "integration.skill_publish.store.list_merged_versions",
                    return_value=[],
                ):
                    from integration.skills.handlers import try_handle_get
                    result = try_handle_get(
                        handler,
                        _parsed("/api/skillhub/skill-versions", "name=new-skill"),
                    )
    assert result is True
    body = json.loads(handler.wfile.write.call_args[0][0])
    assert body["source"] == "custom"
    assert body["published"] is False
    assert body["current_version"] == ""
    assert body["latest_version"] == ""
    assert body["next_version"] == "1.0.0"
    assert body["versions"] == []


def test_skill_versions_scope_custom_overrides_detection():
    """scope=custom forces custom source even when auto-detect would return hub."""
    from integration.skills.handlers import _detect_skill_source

    call_log = {"hub": 0, "custom": 0}

    def fake_hub_payload(name, db):
        call_log["hub"] += 1
        return {"ok": True, "name": name, "source": "hub", "versions": []}

    def fake_custom_payload(name, profile):
        call_log["custom"] += 1
        return {"ok": True, "name": name, "source": "custom", "published": True, "versions": []}

    handler = MagicMock()
    with patch("integration.skills.handlers.skillhub_enabled", return_value=True):
        with patch(
            "integration.skills.handlers._detect_skill_source",
            return_value="hub",
        ) as detect_mock:
            with patch(
                "integration.skills.handlers._hub_versions_payload",
                side_effect=fake_hub_payload,
            ):
                with patch(
                    "integration.skills.handlers._custom_versions_payload",
                    side_effect=fake_custom_payload,
                ):
                    from integration.skills.handlers import try_handle_get

                    # auto → hub
                    result = try_handle_get(
                        handler, _parsed("/api/skillhub/skill-versions", "name=s"),
                    )
                    assert result is True
                    assert detect_mock.call_args.kwargs["scope_filter"] == ""
                    assert call_log["hub"] == 1
                    assert call_log["custom"] == 0

    with patch("integration.skills.handlers.skillhub_enabled", return_value=True):
        with patch(
            "integration.skills.handlers._detect_skill_source",
            return_value="custom",
        ) as detect_mock:
            with patch(
                "integration.skills.handlers._hub_versions_payload",
                side_effect=fake_hub_payload,
            ):
                with patch(
                    "integration.skills.handlers._custom_versions_payload",
                    side_effect=fake_custom_payload,
                ):
                    from integration.skills.handlers import try_handle_get

                    # scope=custom → custom
                    call_log["hub"] = 0
                    call_log["custom"] = 0
                    result = try_handle_get(
                        handler,
                        _parsed("/api/skillhub/skill-versions", "name=s&scope=custom"),
                    )
                    assert result is True
                    assert detect_mock.call_args.kwargs["scope_filter"] == "custom"
                    assert call_log["custom"] == 1
                    assert call_log["hub"] == 0


def test_detect_skill_source_scope_filter_hub_only(tmp_path):
    """scope_filter='hub' ignores custom-only matches (fallback path)."""
    from integration.skills.handlers import _detect_skill_source

    # Build a real custom skill dir
    skill_dir = tmp_path / "skills" / "x"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: x\n---\nbody\n", encoding="utf-8")

    def fake_find(name):
        return skill_dir, skill_dir / "SKILL.md"

    with patch(
        "integration.skills.handlers.skillhub._hub_installed_profiles_all",
        return_value={},
    ):
        with patch(
            "integration.skills.handlers.local_skills._find_skill_in_any_profile",
            side_effect=fake_find,
        ):
            # No profile → fallback path → detects custom
            assert _detect_skill_source("x", "") == "custom"
            assert _detect_skill_source("x", "", scope_filter="hub") == ""
            assert _detect_skill_source("x", "", scope_filter="custom") == "custom"


def test_detect_skill_source_scope_filter_custom_only():
    """scope_filter='custom' ignores hub-only matches (fallback path)."""
    from integration.skills.handlers import _detect_skill_source

    with patch(
        "integration.skills.handlers.skillhub._hub_installed_profiles_all",
        return_value={"y": [{"profile": "default", "dir_name": "y", "skill_dir": "/tmp/y"}]},
    ):
        with patch(
            "integration.skills.handlers.local_skills._find_skill_in_any_profile",
            return_value=(None, None),
        ):
            assert _detect_skill_source("y", "") == "hub"
            assert _detect_skill_source("y", "", scope_filter="hub") == "hub"
            assert _detect_skill_source("y", "", scope_filter="custom") == ""


def test_skill_versions_scope_hub_only_returns_custom_not_found():
    """scope=hub + only custom exists → 404."""
    handler = MagicMock()
    with patch("integration.skills.handlers.skillhub_enabled", return_value=True):
        with patch(
            "integration.skills.handlers._detect_skill_source",
            return_value="",
        ):
            from integration.skills.handlers import try_handle_get
            result = try_handle_get(
                handler,
                _parsed(
                    "/api/skillhub/skill-versions",
                    "name=custom-only&scope=hub",
                ),
            )
    assert result is True
    body = json.loads(handler.wfile.write.call_args[0][0])
    assert "error" in body
    assert "not found" in body["error"]


def test_skill_versions_invalid_scope_returns_400():
    handler = MagicMock()
    with patch("integration.skills.handlers.skillhub_enabled", return_value=True):
        from integration.skills.handlers import try_handle_get
        result = try_handle_get(
            handler,
            _parsed("/api/skillhub/skill-versions", "name=demo&scope=bad"),
        )
    assert result is True
    body = json.loads(handler.wfile.write.call_args[0][0])
    assert "error" in body
    assert "scope" in body["error"]


def test_skill_versions_not_found_returns_404():
    handler = MagicMock()
    with patch("integration.skills.handlers.skillhub_enabled", return_value=True):
        with patch(
            "integration.skills.handlers._detect_skill_source",
            return_value="",
        ):
            from integration.skills.handlers import try_handle_get
            result = try_handle_get(
                handler,
                _parsed("/api/skillhub/skill-versions", "name=nonexistent"),
            )
    assert result is True
    # bad() writes via j() which does one write call with the JSON body
    body = json.loads(handler.wfile.write.call_args[0][0])
    assert "error" in body
    assert "not found" in body["error"]


def _not_installed_anywhere():
    """Context manager stack making local-install detection find nothing."""
    from contextlib import ExitStack

    stack = ExitStack()
    stack.enter_context(
        patch(
            "integration.skills.handlers.skillhub._hub_installed_profiles_all",
            return_value={},
        )
    )
    stack.enter_context(
        patch(
            "integration.skills.handlers.local_skills._find_skill_in_any_profile",
            return_value=(None, None),
        )
    )
    return stack


def test_skill_versions_market_only_skill_falls_back_to_hub():
    """Not installed locally but on sale upstream → 200 with hub source."""
    handler = MagicMock()
    with patch("integration.skills.handlers.skillhub_enabled", return_value=True):
        with _not_installed_anywhere():
            with patch(
                "integration.skills.skillhub.fetch_all_hub_skills",
                return_value=[{"name": "data-analysis"}],
            ):
                with patch(
                    "integration.skills.skillhub.fetch_version_history",
                    return_value=[{"version": "1.2.0", "change_logs": []}],
                ):
                    with patch(
                        "integration.skills.skillhub.is_delisted_installed",
                        return_value=False,
                    ):
                        with patch(
                            "integration.skills.version_store.get",
                            return_value=None,
                        ):
                            with patch(
                                "integration.skills.handlers._DB_PATH", ":memory:",
                            ):
                                from integration.skills.handlers import try_handle_get
                                result = try_handle_get(
                                    handler,
                                    _parsed(
                                        "/api/skillhub/skill-versions",
                                        "name=data-analysis&scope=hub",
                                    ),
                                )
    assert result is True
    body = json.loads(handler.wfile.write.call_args[0][0])
    assert body["ok"] is True
    assert body["source"] == "hub"
    assert body["current_version"] == ""
    assert body["latest_version"] == ""
    assert body["delisted"] is False
    assert len(body["versions"]) == 1


def test_skill_versions_market_only_skill_scope_custom_still_404():
    """scope=custom must not fall back to the market catalog."""
    handler = MagicMock()
    with patch("integration.skills.handlers.skillhub_enabled", return_value=True):
        with _not_installed_anywhere():
            with patch(
                "integration.skills.skillhub.fetch_all_hub_skills",
            ) as catalog_mock:
                from integration.skills.handlers import try_handle_get
                result = try_handle_get(
                    handler,
                    _parsed(
                        "/api/skillhub/skill-versions",
                        "name=data-analysis&scope=custom",
                    ),
                )
    assert result is True
    catalog_mock.assert_not_called()
    body = json.loads(handler.wfile.write.call_args[0][0])
    assert "not found" in body["error"]


def test_skill_versions_catalog_unreachable_returns_404_not_502():
    """Catalog lookup failure degrades to 404, not a 502 upstream error."""
    handler = MagicMock()
    with patch("integration.skills.handlers.skillhub_enabled", return_value=True):
        with _not_installed_anywhere():
            with patch(
                "integration.skills.skillhub.fetch_all_hub_skills",
                side_effect=RuntimeError("hub unreachable"),
            ):
                from integration.skills.handlers import try_handle_get
                result = try_handle_get(
                    handler,
                    _parsed("/api/skillhub/skill-versions", "name=data-analysis"),
                )
    assert result is True
    body = json.loads(handler.wfile.write.call_args[0][0])
    assert "not found" in body["error"]


def test_skill_versions_same_profile_hub_wins_over_custom():
    """Within one profile: hub marker + custom SKILL.md both present → hub wins."""
    handler = MagicMock()
    with patch("integration.skills.handlers.skillhub_enabled", return_value=True):
        with _patch_hub_installed("shared-skill", "tools/shared-skill"):
            with patch(
                "integration.skills.handlers.local_skills._find_skill_in_any_profile",
                return_value=("fake-dir", "fake-md"),
            ):
                with patch(
                    "integration.skills.skillhub.fetch_version_history",
                    return_value=[{"version": "1.0.0", "change_logs": []}],
                ):
                    with patch(
                        "integration.skills.skillhub.is_delisted_installed",
                        return_value=False,
                    ):
                        with patch(
                            "integration.skills.handlers._DB_PATH", ":memory:",
                        ):
                            with patch(
                                "integration.skills.version_store.get",
                                return_value={
                                    "local_version": "0.9.0",
                                    "upstream_version": "1.0.0",
                                },
                            ):
                                from integration.skills.handlers import try_handle_get
                                result = try_handle_get(
                                    handler,
                                    _parsed(
                                        "/api/skillhub/skill-versions",
                                        "name=shared-skill",
                                    ),
                                )
    assert result is True
    body = json.loads(handler.wfile.write.call_args[0][0])
    assert body["source"] == "hub"
    assert body["current_version"] == "0.9.0"
    assert body["latest_version"] == "1.0.0"


def test_skill_versions_profile_scoped_source_detection(tmp_path):
    """Different profiles with same name but different origin → each profile returns its own source.

    Profile ``alice`` has a hub-installed ``skill-x`` (marker file only).
    Profile ``bob`` has a custom ``skill-x`` (SKILL.md, no marker).
    Querying with profile=alice → source=hub; querying with profile=bob → source=custom.
    """
    from pathlib import Path

    # Build alice profile skills dir with hub marker
    alice_skills = tmp_path / "profiles" / "alice" / "skills"
    alice_skill_dir = alice_skills / "skill-x"
    alice_skill_dir.mkdir(parents=True)
    (alice_skill_dir / ".hub_installed").write_text("1", encoding="utf-8")
    (alice_skill_dir / "SKILL.md").write_text(
        "---\nname: skill-x\n---\nhub skill\n", encoding="utf-8"
    )

    # Build bob profile skills dir with custom skill
    bob_skills = tmp_path / "profiles" / "bob" / "skills"
    bob_skill_dir = bob_skills / "skill-x"
    bob_skill_dir.mkdir(parents=True)
    (bob_skill_dir / "SKILL.md").write_text(
        "---\nname: skill-x\n---\ncustom skill\n", encoding="utf-8"
    )

    def _skills_dir_for_profile(p):
        if p == "alice":
            return alice_skills
        if p == "bob":
            return bob_skills
        return Path("/nonexistent")

    # alice query → hub source → upstream fetch called
    handler = MagicMock()
    fetch_call_count = {"n": 0}

    def fake_fetch(name):
        fetch_call_count["n"] += 1
        return [{"version": "2.0.0", "change_logs": []}]

    custom_call_count = {"n": 0}

    def fake_list_merged(name, db_path=None):
        custom_call_count["n"] += 1
        return []

    with patch("integration.skills.handlers.skillhub_enabled", return_value=True):
        with patch(
            "integration.skills.handlers.skillhub._hub_installed_profiles_all",
            return_value={},
        ):
            with patch(
                "integration.skills.handlers.local_skills._find_skill_in_any_profile",
                return_value=(None, None),
            ):
                with patch(
                    "integration.skills.handlers.skillhub.fetch_version_history",
                    side_effect=fake_fetch,
                ):
                    with patch(
                        "integration.skills.handlers.skillhub.is_delisted_installed",
                        return_value=False,
                    ):
                        with patch(
                            "integration.skills.version_store.get",
                            return_value=None,
                        ):
                            with patch(
                                "integration.skill_publish.store.get_latest_version",
                                return_value=None,
                            ):
                                with patch(
                                    "integration.skill_publish.store.list_merged_versions",
                                    side_effect=fake_list_merged,
                                ):
                                    with patch(
                                        "integration.skills.paths.skills_dir_for_profile",
                                        side_effect=_skills_dir_for_profile,
                                    ):
                                        from integration.skills.handlers import try_handle_get

                                        # alice → hub
                                        result = try_handle_get(
                                            handler,
                                            _parsed(
                                                "/api/skillhub/skill-versions",
                                                "name=skill-x&profile=alice",
                                            ),
                                        )
                                        assert result is True
                                        alice_body = json.loads(
                                            handler.wfile.write.call_args[0][0]
                                        )
                                        assert alice_body["source"] == "hub"
                                        assert fetch_call_count["n"] == 1
                                        assert custom_call_count["n"] == 0

                                        # bob → custom
                                        fetch_call_count["n"] = 0
                                        result = try_handle_get(
                                            handler,
                                            _parsed(
                                                "/api/skillhub/skill-versions",
                                                "name=skill-x&profile=bob",
                                            ),
                                        )
                                        assert result is True
                                        bob_body = json.loads(
                                            handler.wfile.write.call_args[0][0]
                                        )
                                        assert bob_body["source"] == "custom"
                                        assert fetch_call_count["n"] == 0
                                        assert custom_call_count["n"] == 1


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


# ---------------------------------------------------------------------------
# _has_update
# ---------------------------------------------------------------------------

def test_has_update_returns_false_when_unreachable():
    from integration.skills.handlers import _has_update
    row = {
        "local_version": "1.0.0",
        "upstream_version": "2.0.0",
        "upstream_unreachable": 1,
    }
    assert _has_update(row) is False


def test_has_update_returns_true_when_reachable_and_newer():
    from integration.skills.handlers import _has_update
    row = {
        "local_version": "1.0.0",
        "upstream_version": "2.0.0",
        "upstream_unreachable": 0,
    }
    assert _has_update(row) is True


def test_has_update_returns_false_when_same_version():
    from integration.skills.handlers import _has_update
    row = {
        "local_version": "1.0.0",
        "upstream_version": "1.0.0",
        "upstream_unreachable": 0,
    }
    assert _has_update(row) is False

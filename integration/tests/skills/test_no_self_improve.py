"""Tests for skills.no_self_improve config and SkillHub lock API."""

from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

import pytest

from integration.skills import no_self_improve
from integration.skills.handlers import try_handle_get, try_handle_post, try_handle_put


def _write_skill(skills_dir, name: str, *, hub: bool = False, category: str = "") -> None:
    if category:
        skill_dir = skills_dir / category / name
    else:
        skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: test\n---\n",
        encoding="utf-8",
    )
    if hub:
        (skill_dir / ".hub_installed").write_text("1", encoding="utf-8")


@pytest.fixture
def config_env(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(no_self_improve, "_config_path", lambda: config_path)
    monkeypatch.setattr(
        "integration.skills.paths.shared_skills_dir",
        lambda: skills_dir,
    )
    monkeypatch.setattr(
        "integration.skills.local_skills.shared_skills_dir",
        lambda: skills_dir,
    )
    with patch("integration.skills.no_self_improve.reload_config"):
        yield {"config_path": config_path, "skills_dir": skills_dir}


def test_normalize_names_dedup_trim():
    assert no_self_improve.normalize_names([" a ", "a", "", "b", " b "]) == ["a", "b"]


def test_save_and_get_round_trip(config_env):
    no_self_improve.save_no_self_improve({"custom-a", "custom-b"})
    assert no_self_improve.get_no_self_improve_names() == {"custom-a", "custom-b"}
    cfg_text = config_env["config_path"].read_text(encoding="utf-8")
    assert "no_self_improve" in cfg_text


def test_add_and_remove_names(config_env):
    no_self_improve.save_no_self_improve(set())
    no_self_improve.add_names(["x"])
    assert "x" in no_self_improve.get_no_self_improve_names()
    no_self_improve.remove_names(["x"])
    assert "x" not in no_self_improve.get_no_self_improve_names()


def test_sync_adds_hub_and_removes_stale(config_env):
    skills_dir = config_env["skills_dir"]
    _write_skill(skills_dir, "hub-one", hub=True)
    _write_skill(skills_dir, "custom-locked", hub=False)
    no_self_improve.save_no_self_improve({"hub-one", "custom-locked", "gone-skill"})
    result = no_self_improve.sync_hub_skills_to_config()
    names = no_self_improve.get_no_self_improve_names()
    assert "gone-skill" not in names
    assert "hub-one" in names
    assert "custom-locked" in names
    assert "gone-skill" in result["removed_stale"]
    assert result["added"] == []


def test_sync_preserves_api_locked_custom(config_env):
    skills_dir = config_env["skills_dir"]
    _write_skill(skills_dir, "my-tool", hub=False)
    no_self_improve.save_no_self_improve({"my-tool"})
    no_self_improve.sync_hub_skills_to_config()
    assert "my-tool" in no_self_improve.get_no_self_improve_names()


def test_apply_lock_fields():
    locked = {"name": "a", "custom": True, "hub_installed": False}
    no_self_improve.apply_lock_fields(locked, {"a"})
    assert locked["no_self_improve"] is True
    assert locked["can_lock"] is True

    hub = {"name": "h", "custom": False, "hub_installed": True}
    no_self_improve.apply_lock_fields(hub, set())
    assert hub["no_self_improve"] is True
    assert hub["can_lock"] is False


def test_get_no_self_improve_route():
    parsed = urlparse("/api/skillhub/skills/no_self_improve")
    handler = MagicMock()
    with patch("integration.skills.handlers.integration_enabled", return_value=True):
        with patch(
            "integration.skills.handlers.no_self_improve.get_no_self_improve_names",
            return_value={"a", "b"},
        ):
            with patch("integration.skills.handlers.j", return_value=True) as j_fn:
                assert try_handle_get(handler, parsed) is True
                payload = j_fn.call_args[0][1]
                assert payload["ok"] is True
                assert payload["count"] == 2


def test_toggle_custom_lock(config_env):
    skills_dir = config_env["skills_dir"]
    _write_skill(skills_dir, "custom-one", hub=False)
    parsed = urlparse("/api/skillhub/skills/no_self_improve/toggle")
    handler = MagicMock()
    body = {"name": "custom-one", "locked": True}
    with patch("integration.skills.handlers.integration_enabled", return_value=True):
        with patch("integration.skills.handlers.j", return_value=True):
            assert try_handle_post(handler, parsed, body) is True
    assert "custom-one" in no_self_improve.get_no_self_improve_names()

    body = {"name": "custom-one", "locked": False}
    with patch("integration.skills.handlers.integration_enabled", return_value=True):
        with patch("integration.skills.handlers.j", return_value=True):
            assert try_handle_post(handler, parsed, body) is True
    assert "custom-one" not in no_self_improve.get_no_self_improve_names()


def test_toggle_hub_returns_403(config_env):
    skills_dir = config_env["skills_dir"]
    _write_skill(skills_dir, "hub-one", hub=True)
    parsed = urlparse("/api/skillhub/skills/no_self_improve/toggle")
    handler = MagicMock()
    body = {"name": "hub-one", "locked": False}
    with patch("integration.skills.handlers.integration_enabled", return_value=True):
        with patch("integration.skills.handlers.bad", return_value=True) as bad_fn:
            assert try_handle_post(handler, parsed, body) is True
            assert bad_fn.call_args.kwargs.get("status") == 403


def test_install_adds_to_config(tmp_path, monkeypatch):
    skills_dir = tmp_path / "skills"
    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr("integration.skills.skillhub.shared_skills_dir", lambda: skills_dir)
    monkeypatch.setattr(no_self_improve, "_config_path", lambda: config_path)
    with patch("integration.skills.no_self_improve.reload_config"):
        with patch("integration.skills.skillhub.download_bytes", side_effect=Exception("no zip")):
            with patch(
                "integration.skills.skillhub.fetch_doc",
                return_value={"content": "---\nname: flat-skill\ndescription: d\n---\n"},
            ):
                from integration.skills import skillhub

                skillhub.install_skill("flat-skill", "Flat", category="")
    assert "flat-skill" in no_self_improve.get_no_self_improve_names()


def test_delete_removes_from_config(config_env):
    skills_dir = config_env["skills_dir"]
    _write_skill(skills_dir, "to-delete", hub=False)
    no_self_improve.save_no_self_improve({"to-delete"})
    from integration.skills import local_skills

    result = local_skills.delete_local_skill("to-delete", "")
    assert result.get("ok") is True
    assert "to-delete" not in no_self_improve.get_no_self_improve_names()


def test_put_replace_names(config_env):
    parsed = urlparse("/api/skillhub/skills/no_self_improve")
    handler = MagicMock()
    body = {"names": ["a", "b"]}
    with patch("integration.skills.handlers.integration_enabled", return_value=True):
        with patch("integration.skills.handlers.j", return_value=True):
            assert try_handle_put(handler, parsed, body) is True
    assert no_self_improve.get_no_self_improve_names() == {"a", "b"}


def test_annotate_installed_lock_fields(tmp_path):
    skills_dir = tmp_path / "skills"
    installed = skills_dir / "hub-skill"
    installed.mkdir(parents=True)
    (installed / "SKILL.md").write_text(
        "---\nname: hub-skill\ndescription: d\n---\n",
        encoding="utf-8",
    )
    (installed / ".hub_installed").write_text("1", encoding="utf-8")

    with patch("integration.skills.skillhub.shared_skills_dir", return_value=skills_dir):
        with patch(
            "integration.skills.no_self_improve.get_no_self_improve_names",
            return_value=set(),
        ):
            from integration.skills import skillhub

            result = skillhub.annotate_installed([{"name": "hub-skill"}])
    assert result[0]["no_self_improve"] is True
    assert result[0]["can_lock"] is False

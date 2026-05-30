"""Local skill delete via POST /api/skillhub/delete."""

from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

import pytest

from integration.skills import local_skills
from integration.skills.handlers import try_handle_post


@pytest.fixture
def skills_root(tmp_path, monkeypatch):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    monkeypatch.setattr("integration.skills.local_skills.shared_skills_dir", lambda: skills_dir)
    monkeypatch.setattr("integration.skills.skillhub.shared_skills_dir", lambda: skills_dir)
    return skills_dir


def test_delete_local_skill_by_name(skills_root):
    skill_dir = skills_root / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: list-name\ndescription: x\n---\n",
        encoding="utf-8",
    )
    result = local_skills.delete_local_skill("list-name")
    assert result.get("ok") is True
    assert result.get("hub_installed") is False
    assert not skill_dir.exists()


def test_delete_local_skill_hub_installed(skills_root):
    skill_dir = skills_root / "hub-one"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\nname: hub-one\ndescription: x\n---\n", encoding="utf-8")
    (skill_dir / ".hub_installed").write_text("1", encoding="utf-8")
    result = local_skills.delete_local_skill("hub-one")
    assert result.get("ok") is True
    assert result.get("hub_installed") is True
    assert not skill_dir.exists()


def test_delete_nested_category_by_dir_name(skills_root):
    skill_dir = skills_root / "apple" / "apple-notes"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: apple-notes\ndescription: notes\n---\n",
        encoding="utf-8",
    )
    result = local_skills.delete_local_skill("apple-notes", "apple/apple-notes")
    assert result.get("ok") is True
    assert result["dir_name"] == "apple/apple-notes"
    assert not skill_dir.exists()


def test_delete_nested_falls_back_when_dir_name_is_leaf_only(skills_root):
    """Legacy list sent dir_name=apple-notes; resolve still finds apple/apple-notes by name."""
    skill_dir = skills_root / "apple" / "apple-notes"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: apple-notes\ndescription: notes\n---\n",
        encoding="utf-8",
    )
    result = local_skills.delete_local_skill("apple-notes", "apple-notes")
    assert result.get("ok") is True
    assert not skill_dir.exists()


def test_delete_route_handler(skills_root):
    skill_dir = skills_root / "x"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\nname: x\ndescription: y\n---\n", encoding="utf-8")
    handler = MagicMock()
    parsed = urlparse("/api/skillhub/delete")
    with patch("integration.skills.handlers.integration_enabled", return_value=True):
        with patch("integration.skills.handlers.j", return_value=True) as j_fn:
            assert try_handle_post(handler, parsed, {"name": "x"}) is True
            j_fn.assert_called_once()
            payload = j_fn.call_args[0][1]
            assert payload.get("ok") is True
    assert not skill_dir.exists()

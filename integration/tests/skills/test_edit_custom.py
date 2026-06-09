"""Custom skill edit via POST /api/skillhub/edit."""

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
    return skills_dir


def test_edit_custom_skill_updates_skill_md(skills_root):
    skill_dir = skills_root / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: my-skill\ndescription: old\n---\n# Old",
        encoding="utf-8",
    )
    result = local_skills.edit_custom_skill(
        name="my-skill",
        content="---\nname: my-skill\ndescription: new\n---\n# New",
    )
    assert result.get("ok") is True
    assert result["name"] == "my-skill"
    assert result["dir_name"] == "my-skill"
    assert result["custom"] is True
    assert (skill_dir / "SKILL.md").read_text(encoding="utf-8").endswith("# New")


def test_edit_nested_custom_skill_by_dir_name(skills_root):
    skill_dir = skills_root / "apple" / "apple-notes"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: apple-notes\ndescription: notes\n---\n# Hi",
        encoding="utf-8",
    )
    (skill_dir / ".category").write_text("apple", encoding="utf-8")
    result = local_skills.edit_custom_skill(
        name="apple-notes",
        dir_name="apple/apple-notes",
        content="---\nname: apple-notes\ndescription: updated\n---\n# Updated",
    )
    assert result.get("ok") is True
    assert result["dir_name"] == "apple/apple-notes"
    assert result["category"] == "apple"


def test_edit_rejects_hub_installed(skills_root):
    skill_dir = skills_root / "hub-one"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: hub-one\ndescription: x\n---\n",
        encoding="utf-8",
    )
    (skill_dir / ".hub_installed").write_text("1", encoding="utf-8")
    result = local_skills.edit_custom_skill(
        name="hub-one",
        content="---\nname: hub-one\ndescription: y\n---\n",
    )
    assert result.get("status") == 403


def test_edit_not_found(skills_root):
    result = local_skills.edit_custom_skill(
        name="missing",
        content="---\nname: missing\ndescription: x\n---\n",
    )
    assert result.get("status") == 404


def test_edit_invalid_content(skills_root):
    skill_dir = skills_root / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: my-skill\ndescription: old\n---\n",
        encoding="utf-8",
    )
    result = local_skills.edit_custom_skill(name="my-skill", content="no frontmatter")
    assert result.get("status") == 400


def test_try_handle_post_edit_route(skills_root):
    skill_dir = skills_root / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: my-skill\ndescription: old\n---\n",
        encoding="utf-8",
    )
    parsed = urlparse("/api/skillhub/edit")
    handler = MagicMock()
    body = {
        "name": "my-skill",
        "content": "---\nname: my-skill\ndescription: new\n---\n# New",
    }
    with patch("integration.skills.handlers.integration_enabled", return_value=True):
        with patch("integration.skills.handlers.j", return_value=True) as j_fn:
            assert try_handle_post(handler, parsed, body) is True
            j_fn.assert_called_once()
            payload = j_fn.call_args.args[1]
            assert payload.get("ok") is True
            assert payload["name"] == "my-skill"

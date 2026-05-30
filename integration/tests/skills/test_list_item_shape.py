"""Aligned fields on GET /api/skillhub/skills list items."""

from unittest.mock import patch

from integration.skills import local_skills, skillhub

_LIST_FIELDS = {
    "name",
    "dir_name",
    "display_name",
    "description",
    "category",
    "version",
    "author",
    "installed",
    "hub_installed",
    "custom",
    "disabled",
}


def test_custom_list_item_includes_hub_aligned_fields(monkeypatch):
    item = {
        "name": "my-skill",
        "dir_name": "tools/my-skill",
        "display_name": "",
        "description": "demo",
        "category": "tools",
        "version": "",
        "author": "",
        "installed": True,
        "hub_installed": False,
        "custom": True,
        "disabled": False,
    }
    monkeypatch.setattr(
        local_skills,
        "_scan_custom_skill_dicts",
        lambda *args, **kwargs: [item],
    )

    payload = local_skills.list_custom_skills("", set())
    result = payload["skills"][0]

    assert set(result) >= _LIST_FIELDS
    assert result["installed"] is True
    assert result["hub_installed"] is False
    assert result["custom"] is True
    assert result["dir_name"] == "tools/my-skill"
    assert result["category"] == "tools"
    assert result["disabled"] is False


def test_hub_list_item_includes_aligned_fields(tmp_path):
    skills_dir = tmp_path / "skills"
    installed = skills_dir / "flat-skill"
    installed.mkdir(parents=True)
    (installed / "SKILL.md").write_text(
        "---\nname: flat-skill\ndescription: d\n---\n",
        encoding="utf-8",
    )
    (installed / ".hub_installed").write_text("1", encoding="utf-8")

    with patch("integration.skills.skillhub.shared_skills_dir", return_value=skills_dir):
        items = skillhub.annotate_installed(
            [
                {"name": "flat-skill", "display_name": "Flat", "description": "d", "category": "tools"},
                {"name": "missing", "display_name": "", "description": "", "category": ""},
            ]
        )

    for item in items:
        assert "dir_name" in item
        assert "installed" in item
        assert "hub_installed" in item
        assert "custom" in item
        assert "disabled" in item

    assert items[0]["installed"] is True
    assert items[0]["dir_name"] == "flat-skill"
    assert items[1]["installed"] is False
    assert items[1]["dir_name"] == ""

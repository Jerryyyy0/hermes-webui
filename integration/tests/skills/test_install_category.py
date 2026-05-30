"""SkillHub install path and annotate_installed (nested category)."""

from unittest.mock import patch

from integration.skills import skillhub


def test_install_skill_flat_without_category(tmp_path, monkeypatch):
    skills_dir = tmp_path / "skills"
    monkeypatch.setattr("integration.skills.skillhub.shared_skills_dir", lambda: skills_dir)

    with patch("integration.skills.skillhub.download_bytes", side_effect=Exception("no zip")):
        with patch(
            "integration.skills.skillhub.fetch_doc",
            return_value={"content": "---\nname: flat-skill\ndescription: d\n---\n"},
        ):
            result = skillhub.install_skill("flat-skill", "Flat", category="")

    assert result.get("ok") is True
    assert result["dir_name"] == "flat-skill"
    assert result["category"] == ""
    target = skills_dir / "flat-skill"
    assert (target / "SKILL.md").is_file()
    assert (target / ".hub_installed").is_file()
    assert not (target / ".category").exists()


def test_install_skill_nested_with_category(tmp_path, monkeypatch):
    skills_dir = tmp_path / "skills"
    monkeypatch.setattr("integration.skills.skillhub.shared_skills_dir", lambda: skills_dir)

    with patch("integration.skills.skillhub.download_bytes", side_effect=Exception("no zip")):
        with patch(
            "integration.skills.skillhub.fetch_doc",
            return_value={"content": "---\nname: data-analysis\ndescription: d\n---\n"},
        ):
            result = skillhub.install_skill("data-analysis", "DA", category="tools")

    assert result.get("ok") is True
    assert result["dir_name"] == "tools/data-analysis"
    assert result["category"] == "tools"
    target = skills_dir / "tools" / "data-analysis"
    assert (target / ".hub_installed").is_file()
    assert (target / ".category").read_text(encoding="utf-8") == "tools"


def test_annotate_installed_nested_hub_path(tmp_path):
    skills_dir = tmp_path / "skills"
    installed = skills_dir / "tools" / "data-analysis"
    installed.mkdir(parents=True)
    (installed / "SKILL.md").write_text("# skill", encoding="utf-8")
    (installed / ".hub_installed").write_text("1", encoding="utf-8")

    with patch("integration.skills.skillhub.shared_skills_dir", return_value=skills_dir):
        result = skillhub.annotate_installed(
            [{"name": "data-analysis"}, {"name": "other"}],
        )

    assert result[0]["installed"] is True
    assert result[0]["hub_installed"] is True
    assert result[0]["dir_name"] == "tools/data-analysis"
    assert result[1]["installed"] is False
    assert result[1]["dir_name"] == ""

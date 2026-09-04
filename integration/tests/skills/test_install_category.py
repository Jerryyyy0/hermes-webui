"""SkillHub install path and annotate_installed (nested category)."""

from unittest.mock import patch

from integration.skills import skillhub
from integration.skills.local_skills import normalize_dir_name


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


def test_install_skill_to_profile_copies_delisted_local_install(tmp_path):
    """Delisted upstream: fall back to copying an install from another profile."""
    src_skills = tmp_path / "other-skills"
    src_dir = src_skills / "govwriter-pro"
    src_dir.mkdir(parents=True)
    (src_dir / "SKILL.md").write_text(
        "---\nname: govwriter-pro\ndescription: d\n---\n", encoding="utf-8"
    )
    (src_dir / "extra.txt").write_text("hello", encoding="utf-8")
    (src_dir / ".hub_installed").write_text("1", encoding="utf-8")
    (src_dir / ".category").write_text("tools", encoding="utf-8")
    (src_dir / ".detail.json").write_text('{"display_name": "GW"}', encoding="utf-8")
    target_skills = tmp_path / "test87-skills"

    def fake_skills_dir(profile_name):
        return target_skills if profile_name == "test87" else src_skills

    with patch("integration.skills.skillhub.skills_dir_for_profile", side_effect=fake_skills_dir):
        with patch("integration.skills.skillhub.download_bytes", side_effect=RuntimeError("upstream 404")):
            with patch("integration.skills.skillhub.fetch_doc", side_effect=RuntimeError("doc 404")):
                with patch(
                    "integration.skills.skillhub._hub_installed_profiles_all",
                    return_value={
                        "govwriter-pro": [
                            {"profile": "default", "dir_name": "govwriter-pro", "skill_dir": src_dir}
                        ]
                    },
                ):
                    with patch(
                        "integration.skills.skillhub.fetch_skill_detail",
                        side_effect=RuntimeError("detail 404"),
                    ):
                        result = skillhub.install_skill_to_profile("govwriter-pro", "test87")

    assert result.get("ok") is True
    assert result["profile"] == "test87"
    target = target_skills / "govwriter-pro"
    assert (target / "SKILL.md").is_file()
    assert (target / "extra.txt").read_text(encoding="utf-8") == "hello"
    assert (target / ".category").read_text(encoding="utf-8") == "tools"
    assert (target / ".detail.json").is_file()
    assert (target / ".hub_installed").is_file()
    assert (target / ".install_name").read_text(encoding="utf-8") == "govwriter-pro"


def test_install_skill_to_profile_no_local_copy_raises(tmp_path):
    """Delisted upstream with no local install: the upstream error propagates."""
    target_skills = tmp_path / "test87-skills"

    with patch("integration.skills.skillhub.skills_dir_for_profile", return_value=target_skills):
        with patch("integration.skills.skillhub.download_bytes", side_effect=RuntimeError("upstream 404")):
            with patch("integration.skills.skillhub.fetch_doc", side_effect=RuntimeError("doc 404")):
                with patch(
                    "integration.skills.skillhub._hub_installed_profiles_all",
                    return_value={},
                ):
                    try:
                        skillhub.install_skill_to_profile("govwriter-pro", "test87")
                        raised = False
                    except RuntimeError:
                        raised = True
    assert raised is True


def test_install_skill_to_profile_skips_source_in_target_profile(tmp_path):
    """A source dir under the target profile's own skills dir is not used for copy.

    The install is caught by the 409 already-installed guard before download;
    the copy fallback's exclude guard additionally protects against aliasing.
    """
    skills_dir = tmp_path / "skills"
    src_dir = skills_dir / "govwriter-pro"
    src_dir.mkdir(parents=True)
    (src_dir / "SKILL.md").write_text(
        "---\nname: govwriter-pro\ndescription: d\n---\n", encoding="utf-8"
    )
    (src_dir / ".hub_installed").write_text("1", encoding="utf-8")

    with patch("integration.skills.skillhub.skills_dir_for_profile", return_value=skills_dir):
        with patch("integration.skills.skillhub.download_bytes", side_effect=RuntimeError("upstream 404")):
            with patch("integration.skills.skillhub.fetch_doc", side_effect=RuntimeError("doc 404")):
                with patch(
                    "integration.skills.skillhub._hub_installed_profiles_all",
                    return_value={
                        "govwriter-pro": [
                            {"profile": "test87", "dir_name": "govwriter-pro", "skill_dir": src_dir}
                        ]
                    },
                ):
                    result = skillhub.install_skill_to_profile("govwriter-pro", "test87")
    assert result.get("status") == 409
    assert not skillhub._copy_existing_local_install(
        "govwriter-pro", skills_dir / "copy-target", exclude_skills_dir=skills_dir
    )


def test_annotate_installed_nested_hub_path(tmp_path):
    skills_dir = tmp_path / "skills"
    installed = skills_dir / "tools" / "data-analysis"
    installed.mkdir(parents=True)
    (installed / "SKILL.md").write_text("# skill", encoding="utf-8")
    (installed / ".hub_installed").write_text("1", encoding="utf-8")

    with patch("api.profiles.list_profiles_api", return_value=[{"name": "default"}]):
        with patch("integration.skills.skillhub.skills_dir_for_profile", return_value=skills_dir):
            result = skillhub.annotate_installed(
                [{"name": "data-analysis"}, {"name": "other"}],
            )

    assert result[0]["installed"] is True
    assert result[0]["hub_installed"] is True
    assert result[0]["dir_name"] == "tools/data-analysis"
    assert result[1]["installed"] is False
    assert result[1]["dir_name"] == ""


def test_annotate_installed_long_catalog_name_matches_truncated_leaf(tmp_path):
    """Hub catalog names longer than 64 chars install to a truncated leaf directory."""
    skills_dir = tmp_path / "skills"
    catalog_name = (
        "Powerpoint---PPTX-slug--powerpoint-pptx-version--1-0-1-homepage--"
        "https---clawic-com-skills-powerpoin"
    )
    leaf = normalize_dir_name(catalog_name)
    installed = skills_dir / "ai-与机器学习" / leaf
    installed.mkdir(parents=True)
    (installed / "SKILL.md").write_text(
        "---\nname: Powerpoint / PPTX\ndescription: d\n---\n",
        encoding="utf-8",
    )
    (installed / ".hub_installed").write_text("1", encoding="utf-8")
    (installed / ".hub_catalog_name").write_text(catalog_name, encoding="utf-8")

    with patch("api.profiles.list_profiles_api", return_value=[{"name": "default"}]):
        with patch("integration.skills.skillhub.skills_dir_for_profile", return_value=skills_dir):
            result = skillhub.annotate_installed([{"name": catalog_name}])

    assert result[0]["installed"] is True
    assert result[0]["dir_name"] == f"ai-与机器学习/{leaf}"


def test_install_skill_writes_hub_catalog_name_sidecar(tmp_path, monkeypatch):
    skills_dir = tmp_path / "skills"
    catalog_name = (
        "Powerpoint---PPTX-slug--powerpoint-pptx-version--1-0-1-homepage--"
        "https---clawic-com-skills-powerpoin"
    )
    monkeypatch.setattr("integration.skills.skillhub.shared_skills_dir", lambda: skills_dir)

    with patch("integration.skills.skillhub.download_bytes", side_effect=Exception("no zip")):
        with patch(
            "integration.skills.skillhub.fetch_doc",
            return_value={
                "content": "---\nname: Powerpoint / PPTX\ndescription: d\n---\n",
            },
        ):
            result = skillhub.install_skill(catalog_name, "ppt生成", category="AI 与机器学习")

    leaf = normalize_dir_name(catalog_name)
    target = skills_dir / "ai-与机器学习" / leaf
    assert result.get("ok") is True
    assert (target / ".hub_catalog_name").read_text(encoding="utf-8") == catalog_name

    with patch("api.profiles.list_profiles_api", return_value=[{"name": "default"}]):
        with patch("integration.skills.skillhub.skills_dir_for_profile", return_value=skills_dir):
            annotated = skillhub.annotate_installed([{"name": catalog_name}])
    assert annotated[0]["installed"] is True

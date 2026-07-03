"""Regression: scan_custom_skills_global must not treat hub_names as category."""

from unittest.mock import patch

from integration.skills import listing, local_skills, skillhub


def test_scan_custom_skills_global_non_empty_hub_names(tmp_path):
    skills_dir = tmp_path / "skills"
    skill = skills_dir / "my-custom"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: my-custom\ndescription: local\n---\n",
        encoding="utf-8",
    )

    with patch("integration.skills.local_skills.shared_skills_dir", return_value=skills_dir):
        result = local_skills.scan_custom_skills_global({"hub-a", "hub-b"})

    assert len(result) == 1
    assert result[0]["name"] == "my-custom"
    assert result[0]["custom"] is True


def test_list_skillhub_skills_custom_scope_with_hub_catalog(tmp_path):
    skills_dir = tmp_path / "skills"
    skill = skills_dir / "local-only"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: local-only\ndescription: mine\n---\n",
        encoding="utf-8",
    )

    ctx = skillhub._HubCatalogContext(
        raw_skills=[{"name": "hub-a"}],
        hub_names={"hub-a"},
        installed_index={},
        annotated_all=[],
        locked_names=set(),
    )

    with patch("integration.skills.listing.skillhub.build_hub_catalog_context", return_value=ctx):
        with patch("integration.skills.local_skills.shared_skills_dir", return_value=skills_dir):
            with patch(
                "integration.skills.no_self_improve.get_no_self_improve_names",
                return_value=set(),
            ):
                result = listing.list_skillhub_skills(scope="custom", all_records=True)

    assert result["total"] == 1
    assert result["skills"][0]["name"] == "local-only"
    assert result["stats"]["custom"] == 1

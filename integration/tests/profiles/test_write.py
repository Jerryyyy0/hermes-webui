"""Profile info write tests."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from integration.profiles.presets import list_logo_presets, preset_logo_data_uri
from integration.profiles.write import save_profile_info


def test_list_logo_presets_has_entries():
    payload = list_logo_presets()
    assert payload["categories"]
    assert len(payload["presets"]) >= 20


def test_preset_logo_data_uri():
    presets = list_logo_presets()["presets"]
    assert presets
    uri = preset_logo_data_uri(presets[0]["id"])
    assert uri and uri.startswith("data:image/png;base64,")


def test_save_profile_info_display_and_preset(tmp_path):
    profile_dir = tmp_path / "work"
    profile_dir.mkdir()
    preset_id = list_logo_presets()["presets"][0]["id"]

    def fake_list():
        return [{"name": "work", "path": str(profile_dir), "skill_count": 0}]

    with patch("api.profiles.list_profiles_api", fake_list), patch(
        "api.profiles.get_active_profile_name", return_value="work"
    ), patch(
        "integration.skills.local_skills.list_installed", return_value={"skills": []}
    ):
        out = save_profile_info(
            "work",
            {"display_name": "Work", "description": "Dev profile", "logo_preset": preset_id},
        )

    assert out["ok"] is True
    info_path = profile_dir / "info.json"
    assert info_path.is_file()
    data = json.loads(info_path.read_text(encoding="utf-8"))
    assert data["display_name"] == "Work"
    assert data["logo"].startswith("data:image/png;base64,")
    assert out["profile"]["info"]["display_name"] == "Work"


def test_save_profile_info_mutually_exclusive(tmp_path):
    profile_dir = tmp_path / "x"
    profile_dir.mkdir()

    def fake_list():
        return [{"name": "x", "path": str(profile_dir), "skill_count": 0}]

    with patch("api.profiles.list_profiles_api", fake_list):
        with pytest.raises(ValueError, match="mutually exclusive"):
            save_profile_info(
                "x",
                {"logo_preset": "abstract-blue-orbit", "logo_base64": "data:image/png;base64,AA=="},
            )


def test_save_remove_logo(tmp_path):
    profile_dir = tmp_path / "work"
    profile_dir.mkdir()
    (profile_dir / "info.json").write_text(
        json.dumps({"display_name": "W", "logo": "data:image/png;base64,AA=="}),
        encoding="utf-8",
    )

    def fake_list():
        return [{"name": "work", "path": str(profile_dir), "skill_count": 0}]

    with patch("api.profiles.list_profiles_api", fake_list), patch(
        "api.profiles.get_active_profile_name", return_value="work"
    ), patch(
        "integration.skills.local_skills.list_installed", return_value={"skills": []}
    ):
        save_profile_info("work", {"remove_logo": True})

    data = json.loads((profile_dir / "info.json").read_text(encoding="utf-8"))
    assert "logo" not in data

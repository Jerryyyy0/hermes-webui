"""Local skill zip download via GET /api/skillhub/download."""

import io
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

import pytest

from integration.skills import local_skills
from integration.skills.handlers import try_handle_get


@pytest.fixture
def skills_root(tmp_path, monkeypatch):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    monkeypatch.setattr("integration.skills.local_skills.shared_skills_dir", lambda: skills_dir)
    return skills_dir


def test_collect_skill_zip_files_excludes_metadata(skills_root):
    skill_dir = skills_root / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\nname: my-skill\ndescription: x\n---\n", encoding="utf-8")
    (skill_dir / ".hub_installed").write_text("1", encoding="utf-8")
    (skill_dir / ".category").write_text("tools", encoding="utf-8")
    (skill_dir / ".install_name").write_text("My Skill", encoding="utf-8")
    (skill_dir / "scripts").mkdir()
    (skill_dir / "scripts" / "run.py").write_text("print(1)\n", encoding="utf-8")

    files, total_bytes, limit_hit = local_skills.collect_skill_zip_files(skill_dir, 1024 * 1024, 100)
    arcnames = {arc for _fp, arc in files}
    assert limit_hit is None
    assert "SKILL.md" in arcnames
    assert "scripts/run.py" in arcnames
    assert ".hub_installed" not in arcnames
    assert ".category" not in arcnames
    assert ".install_name" not in arcnames
    assert total_bytes > 0


def test_prepare_skill_download_success(skills_root):
    skill_dir = skills_root / "apple" / "apple-notes"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: apple-notes\ndescription: notes\n---\n",
        encoding="utf-8",
    )
    result = local_skills.prepare_skill_download("apple-notes", "apple/apple-notes")
    assert result.get("ok") is True
    assert result["zip_basename"] == "apple-notes.zip"
    arcnames = {arc for _fp, arc in result["files"]}
    assert "apple-notes/SKILL.md" in arcnames
    assert len(result["files"]) >= 1


def test_prepare_skill_download_not_found(skills_root):
    result = local_skills.prepare_skill_download("missing")
    assert result.get("status") == 404


def test_prepare_skill_download_system_skill_forbidden(skills_root):
    result = local_skills.prepare_skill_download("hermes")
    assert result.get("status") == 403


def test_try_handle_get_download_route(skills_root):
    skill_dir = skills_root / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: my-skill\ndescription: x\n---\n# Hi",
        encoding="utf-8",
    )
    parsed = urlparse("/api/skillhub/download?name=my-skill")
    handler = MagicMock()
    handler.wfile = io.BytesIO()
    with patch("integration.skills.handlers.integration_enabled", return_value=True):
        with patch("integration.skills.handlers.skillhub_enabled", return_value=False):
            assert try_handle_get(handler, parsed) is True
    handler.send_response.assert_called_with(200)
    zip_bytes = handler.wfile.getvalue()
    assert zip_bytes[:2] == b"PK"
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = zf.namelist()
        assert "my-skill/SKILL.md" in names

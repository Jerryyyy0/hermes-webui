"""Custom skill upload deduplication and round-trip metadata."""

import io
import json
import zipfile
from pathlib import Path

from integration.skills import local_skills


def _zip_bytes(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for path, content in files.items():
            zf.writestr(path, content)
    return buf.getvalue()


def test_find_custom_skill_dirs_by_name_nested_and_flat(tmp_path, monkeypatch):
    skills_dir = tmp_path / "skills"
    nested = skills_dir / "apple" / "apple-reminders"
    wrong = skills_dir / "skill"
    nested.mkdir(parents=True)
    wrong.mkdir(parents=True)
    md = "---\nname: apple-reminders\ndescription: reminders\n---\n"
    (nested / "SKILL.md").write_text(md, encoding="utf-8")
    (wrong / "SKILL.md").write_text(md + "# Updated\n", encoding="utf-8")
    monkeypatch.setattr("integration.skills.local_skills.shared_skills_dir", lambda: skills_dir)

    matches = local_skills.find_custom_skill_dirs_by_name(skills_dir, "apple-reminders")
    paths = sorted(local_skills._skill_dir_rel_path(path, skills_dir) for path in matches)
    assert paths == ["apple/apple-reminders", "skill"]


def test_upload_skill_md_overwrites_by_frontmatter_name(tmp_path, monkeypatch):
    skills_dir = tmp_path / "skills"
    nested = skills_dir / "apple" / "apple-reminders"
    wrong = skills_dir / "skill"
    nested.mkdir(parents=True)
    wrong.mkdir(parents=True)
    (nested / "SKILL.md").write_text(
        "---\nname: apple-reminders\ndescription: old\n---\n# Old\n",
        encoding="utf-8",
    )
    (wrong / "SKILL.md").write_text(
        "---\nname: apple-reminders\ndescription: wrong\n---\n# Wrong\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("integration.skills.local_skills.shared_skills_dir", lambda: skills_dir)

    result = local_skills.upload_custom_skill(
        content="---\nname: apple-reminders\ndescription: new\n---\n# Updated\n",
        filename="SKILL.md",
        overwrite=True,
    )
    assert result.get("ok") is True
    assert result["skills"][0]["dir_name"] == "apple/apple-reminders"
    assert "# Updated" in (nested / "SKILL.md").read_text(encoding="utf-8")
    assert not wrong.exists()


def test_upload_skill_md_ignores_filename_stem(tmp_path, monkeypatch):
    skills_dir = tmp_path / "skills"
    monkeypatch.setattr("integration.skills.local_skills.shared_skills_dir", lambda: skills_dir)
    result = local_skills.upload_custom_skill(
        content="---\nname: apple-reminders\ndescription: x\n---\n# Hi\n",
        filename="SKILL.md",
    )
    assert result.get("ok") is True
    assert result["skills"][0]["dir_name"] == "apple-reminders"
    assert (skills_dir / "apple-reminders" / "SKILL.md").is_file()
    assert not (skills_dir / "skill").exists()


def test_prepare_skill_download_includes_leaf_prefix_and_sidecar(tmp_path, monkeypatch):
    skills_dir = tmp_path / "skills"
    monkeypatch.setattr("integration.skills.local_skills.shared_skills_dir", lambda: skills_dir)
    skill_dir = skills_dir / "apple" / "apple-reminders"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: apple-reminders\ndescription: reminders\n---\n",
        encoding="utf-8",
    )
    (skill_dir / ".category").write_text("apple", encoding="utf-8")

    result = local_skills.prepare_skill_download("apple-reminders", "apple/apple-reminders")
    assert result.get("ok") is True
    assert result["zip_basename"] == "apple-reminders.zip"
    arcnames = {arc for _fp, arc in result["files"]}
    assert "apple-reminders/SKILL.md" in arcnames
    extra = result.get("extra_zip_entries") or []
    assert len(extra) == 1
    assert extra[0][1] == f"apple-reminders/{local_skills._SKILL_ORIGIN_SIDECAR}"
    sidecar = json.loads(extra[0][0].decode("utf-8"))
    assert sidecar["dir_name"] == "apple/apple-reminders"
    assert sidecar["category"] == "apple"
    assert sidecar["name"] == "apple-reminders"


def test_upload_zip_with_sidecar_restores_dir_name(tmp_path, monkeypatch):
    skills_dir = tmp_path / "skills"
    existing = skills_dir / "apple" / "apple-reminders"
    existing.mkdir(parents=True)
    (existing / "SKILL.md").write_text(
        "---\nname: apple-reminders\ndescription: old\n---\n# Old\n",
        encoding="utf-8",
    )
    (existing / ".category").write_text("apple", encoding="utf-8")
    monkeypatch.setattr("integration.skills.local_skills.shared_skills_dir", lambda: skills_dir)

    sidecar = json.dumps(
        {
            "version": 1,
            "dir_name": "apple/apple-reminders",
            "category": "apple",
            "name": "apple-reminders",
        }
    )
    z = _zip_bytes(
        {
            "apple-reminders/SKILL.md": "---\nname: apple-reminders\ndescription: new\n---\n# New\n",
            f"apple-reminders/{local_skills._SKILL_ORIGIN_SIDECAR}": sidecar,
        }
    )
    result = local_skills.upload_custom_skill(category="", zip_bytes=z, filename="apple-reminders.zip", overwrite=True)
    assert result.get("ok") is True
    assert result["skills"][0]["dir_name"] == "apple/apple-reminders"
    assert "# New" in (existing / "SKILL.md").read_text(encoding="utf-8")
    assert not (skills_dir / "apple-reminders").exists()

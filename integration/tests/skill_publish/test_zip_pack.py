"""Tests for skill_publish zip packaging."""

import zipfile

from integration.skill_publish import zip_pack


def _make_skill_dir(tmp_path):
    skill = tmp_path / "demo-skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: demo-skill\n---\nbody", encoding="utf-8")
    (skill / "scripts").mkdir()
    (skill / "scripts" / "run.py").write_text("print('hi')", encoding="utf-8")
    (skill / ".detail.json").write_text("{}", encoding="utf-8")
    (skill / ".user_created").write_text("", encoding="utf-8")
    (skill / "scripts" / ".hidden.py").write_text("", encoding="utf-8")
    return skill


def test_build_skill_zip_excludes_hidden_files(tmp_path):
    skill = _make_skill_dir(tmp_path)
    dest = tmp_path / "out.zip"
    count = zip_pack.build_skill_zip(skill, dest)
    with zipfile.ZipFile(dest) as zf:
        names = sorted(zf.namelist())
    assert names == ["SKILL.md", "scripts/run.py"]
    assert count == 2


def test_build_skill_zip_empty_dir(tmp_path):
    skill = tmp_path / "empty"
    skill.mkdir()
    dest = tmp_path / "out.zip"
    assert zip_pack.build_skill_zip(skill, dest) == 0


def test_create_temp_zip_path_and_safe_unlink(tmp_path):
    path = zip_pack.create_temp_zip_path()
    assert path.exists()
    assert path.name.startswith(zip_pack.TEMP_PREFIX)
    assert path.name.endswith(zip_pack.TEMP_SUFFIX)
    zip_pack.safe_unlink(path)
    assert not path.exists()
    zip_pack.safe_unlink(path)  # tolerant of missing
    zip_pack.safe_unlink(None)


def test_sweep_stale_publish_zips(tmp_path, monkeypatch):
    stale1 = tmp_path / (zip_pack.TEMP_PREFIX + "aaa.zip")
    stale2 = tmp_path / (zip_pack.TEMP_PREFIX + "bbb.zip")
    keep = tmp_path / "other.zip"
    for p in (stale1, stale2, keep):
        p.write_bytes(b"PK")
    monkeypatch.setattr(zip_pack.tempfile, "gettempdir", lambda: str(tmp_path))

    removed = zip_pack.sweep_stale_publish_zips()
    assert removed == 2
    assert not stale1.exists()
    assert not stale2.exists()
    assert keep.exists()


def test_sweep_stale_publish_zips_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(zip_pack.tempfile, "gettempdir", lambda: str(tmp_path))
    assert zip_pack.sweep_stale_publish_zips() == 0

"""Sort and mtime helpers for SkillHub list items."""

from integration.skills.mtime_utils import enrich_skill_mtime, read_local_skill_mtime
from integration.skills.sort_utils import sort_skill_items


def test_sort_skill_items_by_name_asc():
    skills = [
        {"name": "b", "display_name": "Beta"},
        {"name": "a", "display_name": "Alpha"},
    ]
    result = sort_skill_items(skills, sort="name", order="asc")
    assert [s["name"] for s in result] == ["a", "b"]


def test_sort_skill_items_by_name_desc():
    skills = [{"name": "a"}, {"name": "c"}, {"name": "b"}]
    result = sort_skill_items(skills, sort="name", order="desc")
    assert [s["name"] for s in result] == ["c", "b", "a"]


def test_sort_skill_items_by_mtime_desc():
    skills = [
        {"name": "old", "mtime": 1.0},
        {"name": "new", "mtime": 99.0},
        {"name": "missing"},
    ]
    result = sort_skill_items(skills, sort="mtime", order="desc")
    assert [s["name"] for s in result] == ["new", "old", "missing"]


def test_read_local_skill_mtime(tmp_path):
    skills_dir = tmp_path / "skills"
    skill_dir = skills_dir / "tools" / "demo"
    skill_dir.mkdir(parents=True)
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text("---\nname: demo\ndescription: d\n---\n", encoding="utf-8")

    mtime = read_local_skill_mtime(skills_dir, "tools/demo")
    assert mtime is not None
    assert mtime == skill_md.stat().st_mtime


def test_enrich_skill_mtime_prefers_local_when_installed(tmp_path):
    skills_dir = tmp_path / "skills"
    skill_dir = skills_dir / "flat-skill"
    skill_dir.mkdir(parents=True)
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text("---\nname: flat\ndescription: d\n---\n", encoding="utf-8")

    skill = {"name": "flat", "dir_name": "flat-skill", "mtime": 1.0}
    enrich_skill_mtime(skill, skills_dir)
    assert skill["mtime"] == skill_md.stat().st_mtime


def test_enrich_skill_mtime_keeps_upstream_when_not_installed(tmp_path):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    skill = {"name": "remote", "dir_name": "", "mtime": 42.0}
    enrich_skill_mtime(skill, skills_dir)
    assert skill["mtime"] == 42.0


def test_enrich_skill_mtime_maps_updated_at(tmp_path):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    skill = {"name": "remote", "dir_name": "", "updated_at": 55.0}
    enrich_skill_mtime(skill, skills_dir)
    assert skill["mtime"] == 55.0
    assert "updated_at" not in skill


def test_enrich_skill_mtime_local_missing_is_none(tmp_path):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    skill = {"name": "broken", "dir_name": "missing-dir", "mtime": 1.0}
    enrich_skill_mtime(skill, skills_dir)
    assert skill["mtime"] is None

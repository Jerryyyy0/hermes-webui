"""Admin-assigned skillId wrapper dirs: listing recognition + uninstall scope.

管理端直接落盘的技能目录结构（不走安装流程）::

    skills/<skillId>/            # .hub_installed / .install_name / skill.json
      <skill-name>/SKILL.md      # 真正的技能主目录

- 无 SKILL.md 的 skillId 目录不是技能，不得出现在 scope=installed；
- 卸载嵌套布局时连整个 skillId 外壳一起删除（外壳只含一个技能时）。
"""

import json
from unittest.mock import patch

from integration.skills import local_skills, skillhub
from integration.skills.utils import skill_uninstall_root


def _make_stub(skills_dir, skill_id="0b27358f969f467badf"):
    stub = skills_dir / skill_id
    stub.mkdir(parents=True)
    (stub / ".hub_installed").write_text("1", encoding="utf-8")
    (stub / ".install_name").write_text("文件转 Markdown", encoding="utf-8")
    (stub / "skill.json").write_text(
        json.dumps({"skillId": skill_id, "nameSnapshot": "markdown-converter"}),
        encoding="utf-8",
    )
    return stub


def _make_nested(skills_dir, skill_id="0b27358f969f467badf", name="markdown-converter"):
    marker_dir = skills_dir / skill_id
    skill_dir = marker_dir / name
    skill_dir.mkdir(parents=True)
    (marker_dir / ".hub_installed").write_text("1", encoding="utf-8")
    (marker_dir / "skill.json").write_text("{}", encoding="utf-8")
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: d\n---\n", encoding="utf-8"
    )
    return marker_dir, skill_dir


def test_hub_installed_index_skips_stub_without_skill_md(tmp_path):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    _make_stub(skills_dir)
    _make_nested(skills_dir, skill_id="abc123", name="real-skill")

    index = skillhub._hub_installed_index(skills_dir)
    assert "0b27358f969f467badf" not in index
    assert index.get("real-skill") == "abc123/real-skill"


def test_no_phantom_installed_row_for_stub(tmp_path, monkeypatch):
    """The stub must not surface as a synthetic delisted/installed row."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    _make_stub(skills_dir)
    monkeypatch.setattr(
        "integration.skills.skillhub.skills_dir_for_profile", lambda p: skills_dir
    )
    with patch("api.profiles.list_profiles_api", lambda: [{"name": "default"}]):
        profiles = skillhub._hub_installed_profiles_all()
    assert profiles == {}


def test_delete_local_skill_removes_whole_wrapper(tmp_path, monkeypatch):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    marker_dir, skill_dir = _make_nested(skills_dir)
    monkeypatch.setattr(
        "integration.skills.local_skills.shared_skills_dir", lambda: skills_dir
    )

    result = local_skills.delete_local_skill("markdown-converter")
    assert result.get("ok") is True
    assert result.get("hub_installed") is True
    assert result["dir_name"] == "0b27358f969f467badf/markdown-converter"
    assert not marker_dir.exists()


def test_delete_skill_from_profile_removes_whole_wrapper(tmp_path, monkeypatch):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    marker_dir, skill_dir = _make_nested(skills_dir)
    monkeypatch.setattr(
        "integration.skills.skillhub.skills_dir_for_profile", lambda p: skills_dir
    )

    result = skillhub.delete_skill_from_profile("markdown-converter", "default")
    assert result.get("ok") is True
    assert result.get("hub_installed") is True
    assert not marker_dir.exists()


def test_delete_local_skill_stub_by_dir_name(tmp_path, monkeypatch):
    """Hidden stub stays deletable via an explicit dir_name."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    stub = _make_stub(skills_dir)
    monkeypatch.setattr(
        "integration.skills.local_skills.shared_skills_dir", lambda: skills_dir
    )

    result = local_skills.delete_local_skill("whatever", "0b27358f969f467badf")
    assert result.get("ok") is True
    assert result.get("hub_installed") is True
    assert not stub.exists()


def test_multi_skill_wrapper_keeps_sibling(tmp_path, monkeypatch):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    marker_dir, skill_dir = _make_nested(skills_dir, skill_id="wrap", name="skill-a")
    sibling = marker_dir / "skill-b"
    sibling.mkdir()
    (sibling / "SKILL.md").write_text(
        "---\nname: skill-b\ndescription: d\n---\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        "integration.skills.local_skills.shared_skills_dir", lambda: skills_dir
    )

    result = local_skills.delete_local_skill("skill-a")
    assert result.get("ok") is True
    assert not skill_dir.exists()
    assert marker_dir.is_dir()
    assert (sibling / "SKILL.md").is_file()


def test_skill_uninstall_root_levels(tmp_path):
    skills_root = tmp_path / "skills"
    skills_root.mkdir(parents=True)

    flat = skills_root / "flat-skill"
    flat.mkdir()
    (flat / ".hub_installed").write_text("1", encoding="utf-8")
    assert skill_uninstall_root(flat, skills_root) == flat

    plain = skills_root / "plain-skill"
    plain.mkdir()
    assert skill_uninstall_root(plain, skills_root) == plain

    marker_dir = skills_root / "0b27358f969f467badf"
    nested = marker_dir / "markdown-converter"
    nested.mkdir(parents=True)
    (marker_dir / ".hub_installed").write_text("1", encoding="utf-8")
    (nested / "SKILL.md").write_text("---\nname: x\n---\n", encoding="utf-8")
    assert skill_uninstall_root(nested, skills_root) == marker_dir

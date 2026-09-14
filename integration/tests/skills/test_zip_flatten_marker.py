"""Regression tests: zip wrapper promotion + ancestor-aware hub marker checks.

SkillHub download zips bundle a manifest (skill.json) at the archive root
next to the skill folder, which defeats common-prefix flattening and leaves
SKILL.md one level below the install sidecars. The custom-skill scan must
also exclude skills whose .hub_installed marker sits on an ancestor dir.
"""

import io
import zipfile

from integration.skills import local_skills, skillhub
from integration.skills.utils import (
    extract_zip_and_flatten,
    has_hub_installed_marker,
)


def _zip_bytes(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for path, content in files.items():
            zf.writestr(path, content)
    return buf.getvalue()


def test_extract_promotes_nested_skill_dir_beside_root_manifest(tmp_path):
    """Hub zip shape: root skill.json + <skill>/SKILL.md must flatten."""
    target = tmp_path / "install" / "markdown-converter"
    z = _zip_bytes(
        {
            "skill.json": '{"skillId": "abc"}',
            "markdown-converter/SKILL.md": "---\nname: markdown-converter\ndescription: d\n---\n",
            "markdown-converter/skill.json": '{"name": "markdown-converter"}',
        }
    )
    extract_zip_and_flatten(z, target)
    assert (target / "SKILL.md").is_file()
    # nested copy wins over the root manifest on conflicts
    assert (target / "skill.json").read_text(encoding="utf-8") == '{"name": "markdown-converter"}'
    # the nested wrapper dir is gone
    assert not (target / "markdown-converter").exists()


def test_extract_keeps_flat_layout_when_prefix_strips(tmp_path):
    target = tmp_path / "install" / "my-skill"
    z = _zip_bytes(
        {
            "wrapper/SKILL.md": "---\nname: my-skill\ndescription: d\n---\n",
            "wrapper/scripts/run.py": "print(1)\n",
        }
    )
    extract_zip_and_flatten(z, target)
    assert (target / "SKILL.md").is_file()
    assert (target / "scripts" / "run.py").is_file()


def test_extract_no_promotion_without_unambiguous_candidate(tmp_path):
    target = tmp_path / "install" / "multi"
    z = _zip_bytes(
        {
            "readme.txt": "x",
            "a/SKILL.md": "---\nname: a\ndescription: d\n---\n",
            "b/SKILL.md": "---\nname: b\ndescription: d\n---\n",
        }
    )
    extract_zip_and_flatten(z, target)
    assert not (target / "SKILL.md").exists()
    assert (target / "a" / "SKILL.md").is_file()
    assert (target / "b" / "SKILL.md").is_file()


def test_has_hub_installed_marker_levels(tmp_path):
    skills_root = tmp_path / "skills"
    flat = skills_root / "flat-skill"
    flat.mkdir(parents=True)
    (flat / ".hub_installed").write_text("1", encoding="utf-8")
    assert has_hub_installed_marker(flat, skills_root) is True

    nested = skills_root / "cat" / "inner-skill"
    nested.mkdir(parents=True)
    (skills_root / "cat" / ".hub_installed").write_text("1", encoding="utf-8")
    assert has_hub_installed_marker(nested, skills_root) is True

    plain = skills_root / "plain-skill"
    plain.mkdir()
    assert has_hub_installed_marker(plain, skills_root) is False


def test_scan_custom_skills_excludes_marker_on_ancestor(tmp_path, monkeypatch):
    """Corrupted layout: SKILL.md one level under the .hub_installed dir."""
    skills_dir = tmp_path / "skills"
    corrupted = skills_dir / "0b27358f969f467badf" / "markdown-converter"
    corrupted.mkdir(parents=True)
    (skills_dir / "0b27358f969f467badf" / ".hub_installed").write_text("1", encoding="utf-8")
    (corrupted / "SKILL.md").write_text(
        "---\nname: markdown-converter\ndescription: d\n---\n", encoding="utf-8"
    )
    custom_skill = skills_dir / "real-custom"
    custom_skill.mkdir(parents=True)
    (custom_skill / "SKILL.md").write_text(
        "---\nname: real-custom\ndescription: d\n---\n", encoding="utf-8"
    )

    monkeypatch.setattr(
        "integration.skills.local_skills.skills_dir_for_profile", lambda p: skills_dir
    )
    from unittest.mock import patch

    with patch("api.profiles.list_profiles_api", lambda: [{"name": "default"}]):
        result = local_skills.scan_custom_skills_global(set(), profile="default")
    names = [s["name"] for s in result]
    assert "markdown-converter" not in names
    assert "real-custom" in names


def test_hub_installed_index_resolves_nested_skill_md(tmp_path):
    """Corrupted layout: marker dir holds sidecars, SKILL.md nests below.

    dir_name must address the skill's main directory (the one actually
    holding SKILL.md), and the index must resolve the real catalog name from
    the nested SKILL.md without aliasing the install-target dir name (an
    upstream skillId), which would otherwise resurface as a phantom delisted
    entry under scope=installed.
    """
    skills_dir = tmp_path / "skills"
    marker_dir = skills_dir / "0b27358f969f467badf"
    nested = marker_dir / "markdown-converter"
    nested.mkdir(parents=True)
    (marker_dir / ".hub_installed").write_text("1", encoding="utf-8")
    (nested / "SKILL.md").write_text(
        "---\nname: markdown-converter\ndescription: d\n---\n", encoding="utf-8"
    )

    index = skillhub._hub_installed_index(skills_dir)
    assert index.get("markdown-converter") == "0b27358f969f467badf/markdown-converter"
    assert "0b27358f969f467badf" not in index


def test_find_skill_main_dir_descends_wrapper_chain(tmp_path):
    """Skill main dir = the directory directly holding SKILL.md.

    Works for the one-level wrapper (marker dir / skill folder) and for a
    deeper chain (install target / hash dir / skill folder).
    """
    from integration.skills.utils import find_skill_main_dir

    base = tmp_path / "0b27358f969f467badf"
    skill = base / "markdown-converter"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: markdown-converter\ndescription: d\n---\n", encoding="utf-8"
    )
    assert find_skill_main_dir(base) == skill
    assert find_skill_main_dir(skill) == skill

    deeper_base = tmp_path / "install-target"
    deep = deeper_base / "0b27358f969f467badf" / "markdown-converter"
    deep.mkdir(parents=True)
    (deep / "SKILL.md").write_text(
        "---\nname: markdown-converter\ndescription: d\n---\n", encoding="utf-8"
    )
    assert find_skill_main_dir(deeper_base) == deep


def test_hub_installed_index_keeps_leaf_alias_for_normal_layout(tmp_path):
    """Normal layout: dir name differing from frontmatter name stays aliased."""
    skills_dir = tmp_path / "skills"
    skill_dir = skills_dir / "govwriter-pro"
    skill_dir.mkdir(parents=True)
    (skill_dir / ".hub_installed").write_text("1", encoding="utf-8")
    (skill_dir / "SKILL.md").write_text(
        "---\nname: govwriter\ndescription: d\n---\n", encoding="utf-8"
    )

    index = skillhub._hub_installed_index(skills_dir)
    assert index.get("govwriter") == "govwriter-pro"
    assert index.get("govwriter-pro") == "govwriter-pro"

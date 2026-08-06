"""_find_skill miss path must not re-walk the skills index for file-like names."""

from pathlib import Path

import agent.skill_utils as skill_utils
from integration.skills import local_skills


def _write_skill(skills_dir: Path, name: str, *, rel_path: str | None = None) -> None:
    rel = rel_path or name
    skill_dir = skills_dir / rel
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: test\n---\n\n# {name}\n",
        encoding="utf-8",
    )


def test_find_skill_skips_index_walk_for_file_like_miss(tmp_path, monkeypatch):
    skills_dir = tmp_path / "skills"
    for i in range(30):
        _write_skill(skills_dir, f"skill-{i:02d}", rel_path=f"cat/skill-{i:02d}")

    walks: list[int] = []
    original = skill_utils.iter_skill_index_files

    def counting_iter(root, filename):
        walks.append(1)
        yield from original(root, filename)

    monkeypatch.setattr(skill_utils, "iter_skill_index_files", counting_iter)

    skill_dir, skill_md = local_skills._find_skill("work/out-01.md", skills_dir)
    assert skill_dir is None and skill_md is None
    assert walks == []

    walks.clear()
    skill_dir, skill_md = local_skills._find_skill("skill-03", skills_dir)
    assert skill_dir is not None and skill_md is not None
    assert walks  # bare-name resolution still uses the index walk

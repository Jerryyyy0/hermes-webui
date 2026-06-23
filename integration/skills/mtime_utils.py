"""mtime enrichment for SkillHub skill list items."""

from __future__ import annotations

from pathlib import Path

from integration.skills.list_item_shape import upstream_catalog_mtime
from integration.skills.utils import find_skill_main_file, skill_path_within


def read_local_skill_mtime(skills_dir: Path, dir_name: str) -> float | None:
    rel = str(dir_name or "").strip()
    if not rel:
        return None
    candidate = (skills_dir / rel).resolve()
    if not skill_path_within(skills_dir, candidate) or not candidate.is_dir():
        return None
    skill_md = find_skill_main_file(candidate)
    if not skill_md or not skill_md.is_file():
        return None
    try:
        return float(skill_md.stat().st_mtime)
    except OSError:
        return None


def enrich_skill_mtime(skill: dict, skills_dir: Path) -> dict:
    """Set unified ``mtime``; local SKILL.md when installed, else upstream mtime/updated_at."""
    dir_name = str(skill.get("dir_name") or "").strip()
    if dir_name:
        skill["mtime"] = read_local_skill_mtime(skills_dir, dir_name)
    else:
        skill["mtime"] = upstream_catalog_mtime(skill)
    skill.pop("updated_at", None)
    return skill


def enrich_skills_mtime(skills: list[dict], skills_dir: Path) -> list[dict]:
    for skill in skills:
        enrich_skill_mtime(skill, skills_dir)
    return skills

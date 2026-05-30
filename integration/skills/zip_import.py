"""ZIP extraction helpers for multi-skill custom upload."""

from __future__ import annotations

from pathlib import Path


def discover_skill_roots(root: Path) -> list[Path]:
    """Return minimal skill directories (each contains a SKILL.md, not nested in another)."""
    from agent.skill_utils import iter_skill_index_files

    candidates: list[Path] = []
    for skill_md in iter_skill_index_files(root, "SKILL.md"):
        candidates.append(skill_md.parent.resolve())

    unique: list[Path] = []
    seen: set[Path] = set()
    for path in candidates:
        if path not in seen:
            seen.add(path)
            unique.append(path)

    minimal: list[Path] = []
    for candidate in sorted(unique, key=lambda p: (len(p.parts), str(p))):
        if any(candidate != other and candidate.is_relative_to(other) for other in unique):
            continue
        minimal.append(candidate)
    return minimal

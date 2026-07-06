"""Normalized SkillHub list item field shape."""

from __future__ import annotations

_LIST_STRING_FIELDS = (
    "name",
    "dir_name",
    "display_name",
    "display_description",
    "description",
    "category",
    "version",
    "author",
    "icon",
)
_LIST_BOOL_FIELDS = (
    "installed",
    "hub_installed",
    "custom",
    "disabled",
    "no_self_improve",
    "can_lock",
)


def parse_mtime_value(raw: object) -> float | None:
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def upstream_catalog_mtime(skill: dict) -> float | None:
    """Hub catalog: upstream ``mtime`` and ``updated_at`` share the same semantic."""
    for key in ("mtime", "updated_at"):
        parsed = parse_mtime_value(skill.get(key))
        if parsed is not None:
            return parsed
    return None


def normalize_skill_list_item(skill: dict) -> dict:
    """Ensure aligned list fields are always present; empty strings / null mtime when unset."""
    for key in _LIST_STRING_FIELDS:
        skill[key] = str(skill.get(key) or "")
    for key in _LIST_BOOL_FIELDS:
        skill[key] = bool(skill.get(key))
    skill.pop("updated_at", None)
    skill["mtime"] = parse_mtime_value(skill.get("mtime"))
    return skill


def normalize_skill_list_items(skills: list[dict]) -> list[dict]:
    return [normalize_skill_list_item(skill) for skill in skills]

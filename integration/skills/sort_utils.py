"""Sort helpers for SkillHub skill list items."""

from __future__ import annotations

SKILL_LIST_SORT_FIELDS = frozenset({"name", "mtime"})
SKILL_LIST_SORT_ORDERS = frozenset({"asc", "desc"})

_DEFAULT_SORT = "name"
_DEFAULT_ORDER = "asc"


def normalize_sort(raw: str | None) -> str:
    value = str(raw or _DEFAULT_SORT).strip().lower()
    if value in SKILL_LIST_SORT_FIELDS:
        return value
    return value


def normalize_order(raw: str | None) -> str:
    value = str(raw or _DEFAULT_ORDER).strip().lower()
    if value in SKILL_LIST_SORT_ORDERS:
        return value
    return value


def _to_pinyin(text: str) -> str:
    try:
        from pypinyin import lazy_pinyin, Style
        return "".join(lazy_pinyin(text, style=Style.NORMAL)).casefold()
    except Exception:
        return text.casefold()


def _name_sort_key(skill: dict) -> str:
    name = str(skill.get("display_name") or skill.get("name") or "")
    return _to_pinyin(name)


def _mtime_sort_key(skill: dict) -> float:
    raw = skill.get("mtime")
    if raw is None:
        return 0.0
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def sort_skill_items(skills: list[dict], *, sort: str, order: str) -> list[dict]:
    reverse = order == "desc"
    if sort == "mtime":
        key = _mtime_sort_key
    else:
        key = _name_sort_key
    return sorted(skills, key=key, reverse=reverse)

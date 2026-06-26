"""Read/write skills.no_self_improve in the active profile config."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path

from api.config import (
    _cfg_lock,
    _load_yaml_config_file,
    _save_yaml_config_file,
    reload_config,
)

_log = logging.getLogger(__name__)

_MAX_NAMES = 128
_CONFIG_KEY = "no_self_improve"


def _config_path() -> Path:
    from api.routes import _active_profile_config_path

    return _active_profile_config_path()


def normalize_names(names: object) -> list[str]:
    if names is None:
        return []
    if isinstance(names, str):
        names = [names]
    if not isinstance(names, (list, tuple, set)):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for raw in names:
        if not isinstance(raw, str):
            continue
        name = raw.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(name)
        if len(out) >= _MAX_NAMES:
            break
    return out


def get_no_self_improve_names() -> set[str]:
    config_path = _config_path()
    if not config_path.exists():
        return set()
    try:
        cfg = _load_yaml_config_file(config_path)
    except Exception:
        return set()
    skills_cfg = cfg.get("skills")
    if not isinstance(skills_cfg, dict):
        return set()
    return set(normalize_names(skills_cfg.get(_CONFIG_KEY)))


def save_no_self_improve(names: Iterable[str], *, reload: bool = True) -> list[str]:
    normalized = normalize_names(list(names))
    config_path = _config_path()
    with _cfg_lock:
        cfg = _load_yaml_config_file(config_path)
        if "skills" not in cfg or not isinstance(cfg["skills"], dict):
            cfg["skills"] = {}
        cfg["skills"][_CONFIG_KEY] = normalized
        _save_yaml_config_file(config_path, cfg)
    if reload:
        reload_config()
    return normalized


def add_names(names: Iterable[str], *, reload: bool = True) -> set[str]:
    current = get_no_self_improve_names()
    updated = current | set(normalize_names(list(names)))
    save_no_self_improve(updated, reload=reload)
    return updated


def remove_names(names: Iterable[str], *, reload: bool = True) -> set[str]:
    drop = set(normalize_names(list(names)))
    current = get_no_self_improve_names()
    updated = current - drop
    save_no_self_improve(updated, reload=reload)
    return updated


def is_name_locked(name: str, *, hub_installed: bool = False) -> bool:
    if hub_installed:
        return True
    key = str(name or "").strip()
    if not key:
        return False
    return key in get_no_self_improve_names()


def _iter_skill_entries() -> Iterable[tuple[str, bool]]:
    from agent.skill_utils import iter_skill_index_files
    from integration.skills.paths import shared_skills_dir
    from tools.skills_tool import (
        _EXCLUDED_SKILL_DIRS,
        _parse_frontmatter,
        skill_matches_platform,
    )

    skills_dir = shared_skills_dir()
    if not skills_dir.exists():
        return
    seen: set[str] = set()
    search_dirs = [skills_dir]
    try:
        from agent.skill_utils import get_external_skills_dirs

        search_dirs.extend(Path(p) for p in get_external_skills_dirs())
    except Exception:
        pass
    for scan_dir in search_dirs:
        if not scan_dir.exists():
            continue
        for skill_md in iter_skill_index_files(scan_dir, "SKILL.md"):
            if any(part in _EXCLUDED_SKILL_DIRS for part in skill_md.parts):
                continue
            skill_dir = skill_md.parent
            try:
                content = skill_md.read_text(encoding="utf-8")[:4000]
                frontmatter, _ = _parse_frontmatter(content)
                if not skill_matches_platform(frontmatter):
                    continue
                skill_name = str(frontmatter.get("name", skill_dir.name))[:64]
                if skill_name in seen:
                    continue
                seen.add(skill_name)
                hub = (skill_dir / ".hub_installed").is_file()
                yield skill_name, hub
            except Exception as exc:
                _log.debug("skip skill %s: %s", skill_md, exc)


def list_installed_skill_names() -> set[str]:
    return {name for name, _ in _iter_skill_entries()}


def list_hub_installed_skill_names() -> set[str]:
    return {name for name, hub in _iter_skill_entries() if hub}


def sync_hub_skills_to_config(*, dry_run: bool = False) -> dict:
    existing = list_installed_skill_names()
    hub = list_hub_installed_skill_names()
    current = get_no_self_improve_names()
    stale = current - existing
    to_add = hub - current
    final = (current - stale) | to_add
    added = sorted(to_add)
    removed_stale = sorted(stale)
    if not dry_run and final != current:
        save_no_self_improve(final)
    return {"added": added, "removed_stale": removed_stale, "names": sorted(final)}


def apply_lock_fields(skill: dict, locked_names: set[str] | None = None) -> dict:
    hub_installed = bool(skill.get("hub_installed"))
    name = str(skill.get("name") or "").strip()
    custom = bool(skill.get("custom")) or not hub_installed
    if locked_names is None:
        locked_names = get_no_self_improve_names()
    skill["no_self_improve"] = hub_installed or (name in locked_names)
    skill["can_lock"] = custom and not hub_installed
    return skill


def apply_lock_fields_batch(skills: list[dict]) -> list[dict]:
    locked_names = get_no_self_improve_names()
    for skill in skills:
        apply_lock_fields(skill, locked_names)
    return skills

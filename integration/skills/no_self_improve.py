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


def _config_path_for_profile(profile_name: str) -> Path:
    from api.profiles import get_hermes_home_for_profile

    return Path(get_hermes_home_for_profile(profile_name)) / "config.yaml"


def get_no_self_improve_names_for_profile(profile_name: str) -> set[str]:
    config_path = _config_path_for_profile(profile_name)
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


def _save_no_self_improve_for_profile(profile_name: str, names: list[str]) -> None:
    config_path = _config_path_for_profile(profile_name)
    with _cfg_lock:
        cfg = _load_yaml_config_file(config_path)
        if "skills" not in cfg or not isinstance(cfg["skills"], dict):
            cfg["skills"] = {}
        cfg["skills"][_CONFIG_KEY] = names
        _save_yaml_config_file(config_path, cfg)


def add_names_to_profile(profile_name: str, names: Iterable[str]) -> set[str]:
    current = get_no_self_improve_names_for_profile(profile_name)
    updated = current | set(normalize_names(list(names)))
    _save_no_self_improve_for_profile(profile_name, sorted(updated))
    return updated


def remove_names_from_profile(profile_name: str, names: Iterable[str]) -> set[str]:
    drop = set(normalize_names(list(names)))
    current = get_no_self_improve_names_for_profile(profile_name)
    updated = current - drop
    _save_no_self_improve_for_profile(profile_name, sorted(updated))
    return updated


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


def propagate_lock_to_all_profiles(skill_name: str, locked: bool) -> list[str]:
    """Add or remove skill_name from no_self_improve in every profile that has it installed.

    Returns list of profile names that were updated.
    """
    from integration.skills.skillhub import get_skill_installed_profiles

    result = get_skill_installed_profiles(skill_name)
    profiles = result.get("installed") or []
    updated: list[str] = []
    for entry in profiles:
        profile_name = str(entry.get("profile") or "").strip()
        if not profile_name:
            continue
        try:
            if locked:
                add_names_to_profile(profile_name, [skill_name])
            else:
                remove_names_from_profile(profile_name, [skill_name])
            updated.append(profile_name)
        except Exception as exc:
            _log.debug("Failed to update no_self_improve for %s/%s: %s", profile_name, skill_name, exc)
    # Also update the active profile
    try:
        if locked:
            add_names([skill_name])
        else:
            remove_names([skill_name])
    except Exception as exc:
        _log.debug("Failed to update active profile no_self_improve for %s: %s", skill_name, exc)
    # Reload config for the active profile
    try:
        reload_config()
    except Exception:
        pass
    return updated


# ── Per-profile disabled list (skills.disabled) ──────────────────────────

_DISABLED_KEY = "disabled"


def get_disabled_names_for_profile(profile_name: str) -> set[str]:
    config_path = _config_path_for_profile(profile_name)
    if not config_path.exists():
        return set()
    try:
        cfg = _load_yaml_config_file(config_path)
    except Exception:
        return set()
    skills_cfg = cfg.get("skills")
    if not isinstance(skills_cfg, dict):
        return set()
    raw = skills_cfg.get(_DISABLED_KEY)
    if not isinstance(raw, list):
        return set()
    return set(normalize_names(raw))


def _save_disabled_names_for_profile(profile_name: str, names: list[str]) -> None:
    config_path = _config_path_for_profile(profile_name)
    with _cfg_lock:
        cfg = _load_yaml_config_file(config_path)
        if "skills" not in cfg or not isinstance(cfg["skills"], dict):
            cfg["skills"] = {}
        cfg["skills"][_DISABLED_KEY] = names
        _save_yaml_config_file(config_path, cfg)


def _toggle_disabled_in_profile(profile_name: str, skill_name: str, enabled: bool) -> None:
    """Toggle a skill's disabled state in a specific profile's config."""
    current = get_disabled_names_for_profile(profile_name)
    if enabled:
        current.discard(skill_name)
    else:
        current.add(skill_name)
    _save_disabled_names_for_profile(profile_name, sorted(current))


def propagate_skill_toggle(skill_name: str, enabled: bool) -> list[str]:
    """Toggle enabled/disabled state across every profile that has the skill installed.

    Returns list of profile names that were updated.
    """
    from integration.skills.skillhub import get_skill_installed_profiles

    result = get_skill_installed_profiles(skill_name)
    profiles = result.get("installed") or []
    updated: list[str] = []
    for entry in profiles:
        profile_name = str(entry.get("profile") or "").strip()
        if not profile_name:
            continue
        try:
            _toggle_disabled_in_profile(profile_name, skill_name, enabled)
            updated.append(profile_name)
        except Exception as exc:
            _log.debug("Failed to toggle disabled for %s/%s: %s", profile_name, skill_name, exc)
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
    from integration.skills.paths import shared_skills_dir, skills_dir_for_profile
    from tools.skills_tool import (
        _EXCLUDED_SKILL_DIRS,
        _parse_frontmatter,
        skill_matches_platform,
    )

    seen: set[str] = set()

    def _scan(skills_dir: Path) -> Iterable[tuple[str, bool]]:
        if not skills_dir.exists():
            return
        for skill_md in iter_skill_index_files(skills_dir, "SKILL.md"):
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

    # Default profile + external dirs
    search_dirs = [shared_skills_dir()]
    try:
        from agent.skill_utils import get_external_skills_dirs
        search_dirs.extend(Path(p) for p in get_external_skills_dirs())
    except Exception:
        pass
    for scan_dir in search_dirs:
        yield from _scan(scan_dir)
    # Other profiles
    try:
        from api.profiles import list_profiles_api
        for p in list_profiles_api():
            profile_name = str(p.get("name") or "").strip()
            if not profile_name or profile_name == "default":
                continue
            yield from _scan(skills_dir_for_profile(profile_name))
    except Exception:
        pass


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

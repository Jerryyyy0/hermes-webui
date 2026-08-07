"""Startup self-healing for missing/incomplete .detail.json on user-created skills.

Scans user-uploaded (``.user_created``) and session-created (no marker) skills
across all profiles on service startup, re-extracts metadata via the upstream
LLM when ``.detail.json`` is missing or incomplete, and syncs the result to
every profile that contains the same skill. Market-installed skills
(``.hub_installed``) are excluded - their detail comes from upstream at install
time.

Mirrors the daemon-thread startup hook pattern of
``integration.skills.no_self_improve.sync_hub_skills_to_config``.
"""

from __future__ import annotations

import json
import logging
import random
import time
from pathlib import Path

from integration.skills.local_skills import (
    parse_logical_name_from_skill_md,
    read_detail_json,
    save_skill_detail,
)
from integration.skills.paths import skills_dir_for_profile
from integration.skills.skillhub import extract_ai_meta

_log = logging.getLogger(__name__)

REQUIRED_DETAIL_FIELDS = ("name", "description", "detail_json")


def bootstrap_missing_skill_details() -> dict:
    """Scan user-created skills across all profiles; re-extract .detail.json
    via upstream LLM where missing or incomplete, syncing to every profile
    that has the skill installed.

    Returns ``{"scanned": int, "missing": int, "extracted": int, "failed": list[str]}``.
    """
    from integration.config import integration_enabled

    if not integration_enabled():
        return {"scanned": 0, "missing": 0, "extracted": 0, "synced": 0, "failed": []}

    from api.profiles import list_profiles_api

    # logical_name -> [(profile, dir_name, skill_dir), ...]
    locations: dict[str, list[tuple[str, str, Path]]] = {}
    try:
        profiles = list_profiles_api() or []
    except Exception:
        _log.exception("list_profiles_api failed during skill detail bootstrap")
        return {"scanned": 0, "missing": 0, "extracted": 0, "synced": 0, "failed": []}

    for p in profiles:
        profile_name = str(p.get("name") or "").strip()
        if not profile_name:
            continue
        try:
            skills_dir = skills_dir_for_profile(profile_name)
        except Exception:
            continue
        if not skills_dir.exists():
            continue
        for skill_dir in _iter_skill_dirs(skills_dir):
            skill_md = skill_dir / "SKILL.md"
            if not skill_md.is_file():
                continue
            if (skill_dir / ".hub_installed").is_file():
                continue
            logical_name = parse_logical_name_from_skill_md(skill_md)
            if not logical_name:
                continue
            dir_name = _relative_dir_name(skill_dir, skills_dir)
            locations.setdefault(logical_name, []).append(
                (profile_name, dir_name, skill_dir)
            )

    scanned = len(locations)
    missing = 0
    extracted = 0
    synced = 0
    failed: list[str] = []

    for skill_name, locs in locations.items():
        # Phase 1: scan all locations - find a complete source and the incomplete ones.
        # If any profile already has a complete .detail.json, sync it to the rest
        # instead of calling upstream LLM.
        complete_detail: dict | None = None
        source_profile = ""
        incomplete_locs: list[tuple[str, str, Path]] = []
        for profile_name, dir_name, skill_dir in locs:
            detail = read_detail_json(skill_dir)
            if _is_detail_complete(detail):
                if complete_detail is None:
                    complete_detail = detail
                    source_profile = profile_name
            else:
                incomplete_locs.append((profile_name, dir_name, skill_dir))

        if not incomplete_locs:
            continue

        missing += 1

        # Phase 2a: sync from an existing complete profile (no upstream call).
        if complete_detail is not None:
            completed = _save_to_all_profiles(skill_name, complete_detail, incomplete_locs)
            if completed:
                synced += 1
                _log.info(
                    "skill detail bootstrap: synced %s from profile %s to %d profile(s)",
                    skill_name, source_profile, len(completed),
                )
                continue
            # Sync made no progress (all saves failed and none were pre-complete);
            # fall through to upstream extraction as a fallback.

        # Phase 2b: no complete source (or sync failed) - extract via upstream.
        canonical_profile, _, canonical_dir = locs[0]
        try:
            skill_md_content = (canonical_dir / "SKILL.md").read_text(encoding="utf-8")
        except Exception:
            _log.warning(
                "skill detail bootstrap: cannot read SKILL.md for %s in profile %s",
                skill_name, canonical_profile,
            )
            failed.append(skill_name)
            continue

        category = _read_category_marker(canonical_dir)
        description = _parse_skill_description(skill_md_content)
        ai_meta = _extract_with_retry(skill_md_content, skill_name, description)
        if ai_meta is None:
            failed.append(skill_name)
            continue

        payload = _build_detail_payload(skill_name, ai_meta, category)
        completed = _save_to_all_profiles(skill_name, payload, incomplete_locs)
        if not completed:
            failed.append(skill_name)
            continue
        extracted += 1

    return {
        "scanned": scanned,
        "missing": missing,
        "extracted": extracted,
        "synced": synced,
        "failed": failed,
    }


def _is_detail_complete(detail: dict | None) -> bool:
    if not isinstance(detail, dict):
        return False
    for field in REQUIRED_DETAIL_FIELDS:
        value = detail.get(field)
        if value is None:
            return False
        if isinstance(value, str) and not value.strip():
            return False
        if isinstance(value, (dict, list)) and not value:
            return False
    return True


def _extract_with_retry(
    skill_md_content: str,
    name: str,
    description: str,
    *,
    max_retries: int = 3,
    base_delay: float = 1.0,
) -> dict | None:
    """Call extract_ai_meta with exponential backoff + ±50% jitter.

    Backoff schedule (base_delay=1.0, 3 attempts -> 2 sleeps):
      after attempt 1: 0.5-1.0s
      after attempt 2: 1.0-2.0s
    Jitter spreads concurrent retries across multi-instance deployments so they
    don't synchronously hammer the upstream SkillHub.
    """
    start = time.monotonic()
    last_error = "unknown"
    for attempt in range(1, max_retries + 1):
        try:
            result = extract_ai_meta(skill_md_content, name=name, description=description)
            if isinstance(result, dict) and result.get("detailJson"):
                elapsed = time.monotonic() - start
                _log.info(
                    "skill detail bootstrap: extracted %s in %.2fs (attempt %d/%d)",
                    name, elapsed, attempt, max_retries,
                )
                return result
            last_error = "empty result or missing detailJson"
        except Exception as exc:
            last_error = str(exc) or exc.__class__.__name__
        if attempt < max_retries:
            delay = base_delay * (2 ** (attempt - 1)) * (0.5 + random.random())
            time.sleep(delay)
    elapsed = time.monotonic() - start
    _log.warning(
        "skill detail bootstrap: extraction failed for %s after %d attempts in %.2fs: %s",
        name, max_retries, elapsed, last_error,
    )
    return None


def _build_detail_payload(skill_name: str, ai_meta: dict, category: str) -> dict:
    return {
        "name": ai_meta.get("name") or skill_name,
        "description": ai_meta.get("description") or "",
        "display_name": ai_meta.get("skillName") or skill_name,
        "display_description": ai_meta.get("displayDescription") or "",
        "detail_json": ai_meta.get("detailJson") or {},
        "category": category or "",
    }


def _save_to_all_profiles(
    skill_name: str,
    detail: dict,
    locations: list[tuple[str, str, Path]],
) -> list[str]:
    """Save .detail.json to each (profile, dir_name, skill_dir).

    Re-reads .detail.json before each write: if it is now complete (filled by
    a concurrent caller such as the frontend's /detail endpoint during the
    extraction window), skip that profile instead of overwriting. Returns the
    list of profiles where .detail.json is now complete (saved by us or
    already complete from re-check).
    """
    completed: list[str] = []
    for profile_name, dir_name, skill_dir in locations:
        if _is_detail_complete(read_detail_json(skill_dir)):
            _log.info(
                "skill detail bootstrap: skip save for %s in profile %s "
                "(.detail.json became complete during extraction)",
                skill_name, profile_name,
            )
            completed.append(profile_name)
            continue
        try:
            result = save_skill_detail(skill_name, detail, dir_name, profile=profile_name)
            if isinstance(result, dict) and result.get("ok"):
                completed.append(profile_name)
            else:
                _log.warning(
                    "skill detail bootstrap: save failed for %s in profile %s: %s",
                    skill_name, profile_name, result,
                )
        except Exception:
            _log.exception(
                "skill detail bootstrap: save raised for %s in profile %s",
                skill_name, profile_name,
            )
    return completed


def _iter_skill_dirs(skills_dir: Path):
    """Yield skill directories (containing SKILL.md) anywhere under skills_dir.

    Uses agent.skill_utils.iter_skill_index_files so category-nested layouts
    like ``skills/<category>/<skill>/SKILL.md`` are handled the same way as
    the existing _scan_custom_skill_dicts scan.
    """
    try:
        from agent.skill_utils import iter_skill_index_files

        for skill_md in iter_skill_index_files(skills_dir, "SKILL.md"):
            yield skill_md.parent
    except Exception:
        _log.debug("failed to iterate skills dir %s", skills_dir, exc_info=True)


def _read_category_marker(skill_dir: Path) -> str:
    p = skill_dir / ".category"
    if not p.is_file():
        return ""
    try:
        return p.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def _parse_skill_description(content: str) -> str:
    """Extract description from SKILL.md: frontmatter ``description`` field,
    falling back to the first non-heading body line. Mirrors the pattern in
    ``local_skills._scan_custom_skill_dicts``.
    """
    try:
        from tools.skills_tool import _parse_frontmatter

        frontmatter, body = _parse_frontmatter(str(content or "")[:4000])
        desc = str(frontmatter.get("description", "") or "").strip()
        if desc:
            return desc
        for line in body.strip().split("\n"):
            line = line.strip()
            if line and not line.startswith("#"):
                return line
    except Exception:
        _log.debug("failed to parse skill description", exc_info=True)
    return ""


def _relative_dir_name(skill_dir: Path, skills_dir: Path) -> str:
    try:
        rel = skill_dir.resolve().relative_to(skills_dir.resolve())
        return str(rel).replace("\\", "/")
    except Exception:
        return skill_dir.name

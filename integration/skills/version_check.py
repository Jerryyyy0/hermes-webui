"""Check for updates on all SkillHub-installed skills.

Called at server startup and when the skill-hub panel is opened.
Updates are written to the version_store DB; auto-upgrade is performed
when ``skills_auto_update`` is enabled in settings.
"""

from __future__ import annotations

import logging
from typing import Any

_log = logging.getLogger(__name__)


def check_updates_for_installed_skills(db_path=None) -> dict:
    """Batch-check all installed skills for upstream updates.

    Returns ``{checked: int, upgradable: int, auto_updated: int}``.
    Never raises -- all errors are caught and logged.
    """
    from api.config import load_settings
    from integration.skills.skillhub import (
        _hub_installed_index_all_profiles,
        fetch_versions_batch,
        upgrade_skill,
    )
    from integration.skills.version_store import (
        list_upgradable,
        mark_upstream_unreachable,
        normalize_change_logs,
        refresh_upstream,
    )

    names = list(_hub_installed_index_all_profiles().keys())
    if not names:
        return {"checked": 0, "upgradable": 0, "auto_updated": 0}

    # Batch fetch upstream versions
    try:
        batch_result = fetch_versions_batch(names)
    except Exception as exc:
        _log.warning("skill update check failed: %s", exc)
        # Mark all unreachable so red-dot doesn't flicker
        for n in names:
            try:
                mark_upstream_unreachable(n, db_path=db_path)
            except Exception:
                pass
        return {"checked": len(names), "upgradable": 0, "auto_updated": 0}

    items = batch_result.get("items") or []
    missing = batch_result.get("missing") or []

    # Update DB with upstream info
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        try:
            change_logs = normalize_change_logs(item.get("change_logs"))
            refresh_upstream(
                name,
                upstream_version=str(item.get("version") or ""),
                change_logs=change_logs,
                change_log_updated_at=str(item.get("change_log_updated_at") or ""),
                published_at=str(item.get("published_at") or ""),
                db_path=db_path,
            )
        except Exception as exc:
            _log.debug("refresh_upstream failed for %s: %s", name, exc)

    for name in missing:
        try:
            mark_upstream_unreachable(name, db_path=db_path)
        except Exception:
            pass

    # Count upgradable
    upgradable_list: list[dict] = []
    try:
        upgradable_list = list_upgradable(db_path=db_path)
        upgradable = len(upgradable_list)
    except Exception:
        upgradable = 0

    # Auto-update if enabled
    auto_updated = 0
    try:
        settings = load_settings()
        if settings.get("skills_auto_update"):
            for row in upgradable_list:
                name = row.get("catalog_name") or ""
                if not name:
                    continue
                try:
                    result = upgrade_skill(name, action="auto_upgrade")
                    if result.get("ok"):
                        auto_updated += 1
                except Exception as exc:
                    _log.warning("auto-upgrade failed for %s: %s", name, exc)
            # Recount after auto-upgrade
            upgradable = len(list_upgradable(db_path=db_path))
    except Exception as exc:
        _log.debug("auto-update check failed: %s", exc)

    _log.info(
        "skill update check: checked=%d, upgradable=%d, auto_updated=%d",
        len(names), upgradable, auto_updated,
    )
    return {
        "checked": len(names),
        "upgradable": upgradable,
        "auto_updated": auto_updated,
    }

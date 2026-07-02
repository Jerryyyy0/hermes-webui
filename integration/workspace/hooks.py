"""Register workspace integration hooks (session save cache invalidation)."""

from __future__ import annotations

import logging
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

_installed = False


def _on_session_saved(session) -> None:
    from integration.config import integration_enabled

    if not integration_enabled():
        return
    try:
        workspace = Path(str(session.workspace)).expanduser().resolve()
    except (TypeError, ValueError, OSError):
        return

    from integration.workspace.file_index_cache import invalidate_workspace_file_index

    invalidate_workspace_file_index(workspace)


def _run_profile_backfill() -> None:
    try:
        from api.session_manifest_store import (
            backfill_empty_profile_artifacts,
            backfill_workspace_artifacts_from_sessions,
        )

        # B-class first: extract artifact rows for sessions that have none.
        # New rows inherit session.profile (non-empty for profiled sessions),
        # so the A-class pass below only needs to patch legacy empty-profile
        # rows that B-class cannot reach (sessions whose own profile is empty).
        b_result = backfill_workspace_artifacts_from_sessions()
        b_written = int(b_result.get("written") or 0)
        if b_written:
            logger.info(
                "backfilled %d artifact rows across %d sessions",
                b_written,
                int(b_result.get("sessions") or 0),
            )

        a_result = backfill_empty_profile_artifacts()
        a_patched = int(a_result.get("patched") or 0)
        if a_patched:
            logger.info("patched %d empty-profile artifact rows", a_patched)
    except Exception:
        logger.debug("workspace profile backfill failed", exc_info=True)


def install_workspace_integration_hooks() -> None:
    """Patch Session.save to invalidate integration workspace caches."""
    global _installed
    if _installed:
        return
    _installed = True

    from integration.config import integration_enabled

    if not integration_enabled():
        return

    from api.models import Session

    original_save = Session.save

    def patched_save(self, *args, **kwargs):
        original_save(self, *args, **kwargs)
        try:
            _on_session_saved(self)
        except Exception:
            logger.debug("Workspace integration session-save hook failed", exc_info=True)

    Session.save = patched_save

    threading.Thread(
        target=_run_profile_backfill,
        name="workspace-profile-backfill",
        daemon=True,
    ).start()

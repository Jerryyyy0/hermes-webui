"""Register workspace integration hooks (session save cache invalidation)."""

from __future__ import annotations

import logging
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
    # Profile artifact index maintenance temporarily disabled (scheme C).
    # from integration.workspace.artifact_profiles import update_workspace_artifact_profile_for_session
    # try:
    #     update_workspace_artifact_profile_for_session(session, workspace_root=workspace)
    # except Exception:
    #     logger.debug(
    #         "Failed to incrementally update workspace artifact profile index",
    #         exc_info=True,
    #     )


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

"""HTTP handler for session read-state updates."""

from __future__ import annotations

import sqlite3

from api.helpers import bad, j
from api.models import get_session
from api.profiles import get_active_profile_name

from integration.config import integration_enabled

from .store import mark_session_read

_PATH = "/api/integration/sessions/mark_read"


def try_handle_post(handler, parsed, body) -> bool:
    if parsed.path != _PATH or not integration_enabled():
        return False

    session_id = str((body or {}).get("session_id") or "").strip()
    if not session_id:
        bad(handler, "session_id 必填", status=400)
        return True

    try:
        session = get_session(session_id)
    except KeyError:
        bad(handler, "会话不存在", status=404)
        return True

    try:
        payload = mark_session_read(get_active_profile_name(), session)
    except (OSError, sqlite3.Error):
        bad(handler, "已读状态暂不可用", status=503)
        return True

    j(handler, payload)
    return True

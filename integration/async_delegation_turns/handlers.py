"""HTTP control endpoints for WebUI-owned asynchronous delegations."""

from __future__ import annotations

import threading
from urllib.parse import parse_qs

from api.helpers import bad, j
from api.models import get_session
from api.config import _get_session_agent_lock

from .state import begin_async_delegation_cancellation, cancellation_status


_CANCEL_PATH = "/api/sessions/background_tasks/cancel"


def _visible_session(session, handler) -> bool:
    try:
        from api.routes import _session_visible_to_active_profile

        return _session_visible_to_active_profile(getattr(session, "profile", None), handler)
    except Exception:
        return False


def _session_or_error(handler, session_id: str):
    try:
        session = get_session(session_id)
    except KeyError:
        bad(handler, "会话不存在", status=404)
        return None
    if not _visible_session(session, handler):
        bad(handler, "会话不存在", status=404)
        return None
    return session


def _interrupt_in_background(session_id: str, delegation_ids: list[str]) -> None:
    try:
        from tools.async_delegation import interrupt_delegations

        interrupt_delegations(delegation_ids, reason=f"webui_session_cancel:{session_id}")
    except (ImportError, AttributeError):
        # Older Agent builds cannot target individual delegation IDs. The
        # cancellation barrier remains durable; their completions still settle
        # normally, but no unsafe session-wide interrupt is attempted.
        return
    except Exception:
        return


def try_handle_get(handler, parsed) -> bool:
    if parsed.path != _CANCEL_PATH:
        return False
    query = parse_qs(parsed.query or "", keep_blank_values=True)
    session_id = str((query.get("session_id") or [""])[0] or "").strip()
    if not session_id:
        bad(handler, "session_id 必填", status=400)
        return True
    session = _session_or_error(handler, session_id)
    if session is None:
        return True
    payload = cancellation_status(session)
    payload["session_id"] = session_id
    j(handler, payload)
    return True


def try_handle_post(handler, parsed, body) -> bool:
    if parsed.path != _CANCEL_PATH:
        return False
    session_id = str((body or {}).get("session_id") or "").strip()
    if not session_id:
        bad(handler, "session_id 必填", status=400)
        return True
    session = _session_or_error(handler, session_id)
    if session is None:
        return True
    try:
        with _get_session_agent_lock(session_id):
            cancellation = begin_async_delegation_cancellation(session)
    except Exception:
        bad(handler, "取消状态暂时无法保存", status=500)
        return True

    payload = {"session_id": session_id, **cancellation}
    if cancellation["state"] == "idle":
        j(handler, payload)
        return True

    # The barrier is already durable before the interrupt request is sent. Do
    # not wait for child processes; their normal completion events close it.
    threading.Thread(
        target=_interrupt_in_background,
        args=(session_id, list(cancellation["delegation_ids"])),
        name=f"hermes-webui-cancel-{session_id[:8]}",
        daemon=True,
    ).start()
    j(handler, payload, status=202)
    return True

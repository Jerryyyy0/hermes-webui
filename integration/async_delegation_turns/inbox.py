"""Durable async-delegation inbox scheduler owned by the WebUI."""

from __future__ import annotations

import heapq
import logging
import threading
import time

logger = logging.getLogger(__name__)

_LOCK = threading.RLock()
_WAKE = threading.Condition(_LOCK)
_HEAP: list[tuple[float, int, str]] = []
_TICKETS: dict[str, int] = {}
_TICKET = 0
_STOP = threading.Event()
_THREAD: threading.Thread | None = None
_SAFETY_SWEEP_SECONDS = 45.0
_ACK_RETRY_SECONDS = 5.0


def _sidecar_session_ids() -> list[str]:
    from api.config import SESSION_DIR

    return [
        path.stem
        for path in SESSION_DIR.glob("*.json")
        if not path.name.startswith("_")
    ]


def _next_ticket(session_id: str) -> int:
    global _TICKET
    _TICKET += 1
    _TICKETS[session_id] = _TICKET
    return _TICKET


def notify(session_id: str, *, delay: float = 0.0) -> bool:
    sid = str(session_id or "").strip()
    if not sid:
        return False
    due = time.monotonic() + max(0.0, float(delay))
    with _WAKE:
        ticket = _next_ticket(sid)
        heapq.heappush(_HEAP, (due, ticket, sid))
        _WAKE.notify()
    return True


def _pop_due() -> str | None:
    now = time.monotonic()
    while _HEAP:
        due, ticket, sid = _HEAP[0]
        if due > now:
            return None
        heapq.heappop(_HEAP)
        if _TICKETS.get(sid) != ticket:
            continue
        _TICKETS.pop(sid, None)
        return sid
    return None


def _load_session(sid: str):
    from api.models import Session

    return Session.load(sid)


def _backend_supported(session) -> bool:
    from api.config import get_config
    from api.gateway_chat import webui_gateway_chat_enabled

    if webui_gateway_chat_enabled(get_config()):
        return False
    try:
        from api.runtime_adapter import runtime_adapter_runner_enabled

        if runtime_adapter_runner_enabled():
            return False
    except Exception:
        pass
    return True


def _reconcile_ack_pending(session) -> bool:
    from integration.async_delegation_turns.delivery_scope import session_delivery_scope

    with session_delivery_scope(session):
        return _reconcile_ack_pending_scoped(session)


def _reconcile_ack_pending_scoped(session) -> bool:
    """Resolve ACK state that may have crossed a server restart."""
    records = getattr(session, "async_delegation_origins", None)
    if not isinstance(records, dict):
        return False
    try:
        import importlib

        delivery = importlib.import_module("tools.async_delegation")
        get_durable = getattr(delivery, "get_durable_delegation", None)
    except Exception:
        return False
    from integration.async_delegation_turns import mark_completion_ack
    if not callable(get_durable):
        # Explicit legacy capability: there is no durable row API to consult.
        # Preserve that uncertainty in the existing error_code, but allow the
        # WebUI-owned payload to progress instead of retrying an impossible read.
        changed = False
        for delegation_id, raw in list(records.items()):
            if not isinstance(raw, dict) or raw.get("wakeup_state") != "queued":
                continue
            wakeup = raw.get("wakeup") if isinstance(raw.get("wakeup"), dict) else {}
            if wakeup.get("error_code") != "agent_ack_pending":
                continue
            mark_completion_ack(
                session,
                str(delegation_id),
                acknowledged=False,
                unknown=True,
            )
            changed = True
        return changed
    changed = False
    for delegation_id, raw in list(records.items()):
        if not isinstance(raw, dict) or raw.get("wakeup_state") != "queued":
            continue
        wakeup = raw.get("wakeup") if isinstance(raw.get("wakeup"), dict) else {}
        if wakeup.get("error_code") != "agent_ack_pending":
            continue
        try:
            durable = get_durable(str(delegation_id))
        except Exception:
            continue
        if not isinstance(durable, dict):
            # A confirmed missing row after WebUI's durable ownership cutover
            # normally means Agent already delivered and later pruned it.  It
            # cannot be proven acknowledged, but blocking forever is worse and
            # the sidecar payload is now authoritative.
            mark_completion_ack(
                session,
                str(delegation_id),
                acknowledged=False,
                unknown=True,
            )
            changed = True
            continue
        state = str(durable.get("delivery_state") or "").strip().lower()
        if state == "delivered":
            mark_completion_ack(session, str(delegation_id), acknowledged=True)
            changed = True
        elif state and state not in {"pending", "claimed", "in_progress"}:
            mark_completion_ack(session, str(delegation_id), acknowledged=False, unknown=True)
            changed = True
    return changed


def _has_ack_pending(session) -> bool:
    records = getattr(session, "async_delegation_origins", None)
    if not isinstance(records, dict):
        return False
    for raw in records.values():
        if not isinstance(raw, dict) or raw.get("wakeup_state") != "queued":
            continue
        wakeup = raw.get("wakeup") if isinstance(raw.get("wakeup"), dict) else {}
        if wakeup.get("error_code") == "agent_ack_pending":
            return True
    return False


def _drain_one(sid: str) -> None:
    from api.config import _get_session_agent_lock
    from api.routes import drain_pending_chat_turn, start_session_turn
    from integration.pending_chat_turns import pending_turns
    from integration.async_delegation_turns import (
        mark_async_delegation_wakeup,
        select_next_queued_wakeup,
    )

    user_turn_waiting = False
    with _get_session_agent_lock(sid):
        session = _load_session(sid)
        if session is None:
            return
        if bool(getattr(session, "archived", False)):
            notify(sid, delay=60.0)
            return
        _reconcile_ack_pending(session)
        selected = select_next_queued_wakeup(session)
        if selected is None:
            if _has_ack_pending(session):
                notify(sid, delay=_ACK_RETRY_SECONDS)
            return
        delegation_id, record = selected
        if getattr(session, "active_stream_id", None):
            notify(sid, delay=1.0)
            return
        user_turn_waiting = any(
            str(item.get("status") or "") in {"queued", "dispatching"}
            for item in pending_turns(session)
        )
        if user_turn_waiting:
            prompt = ""
            turn_key = ""
        else:
            wakeup = dict(record.get("wakeup") or {})
            prompt = str(wakeup.get("prompt") or "").strip()
            turn_key = str(record.get("turn_key") or "").strip()
            previous_attempts = max(0, int(wakeup.get("start_attempts") or 0))
        if not user_turn_waiting and not _backend_supported(session):
            mark_async_delegation_wakeup(
                session,
                delegation_id,
                wakeup_state="queued",
                content="",
                error_code="backend_unsupported",
            )
            logger.info(
                "async_delegation_wakeup_backend_deferred session_id=%s delegation_id=%s",
                sid,
                delegation_id,
            )
            notify(sid, delay=60.0)
            return

    if user_turn_waiting:
        drain_pending_chat_turn(sid)
        notify(sid, delay=1.0)
        return

    try:
        result = start_session_turn(
            sid,
            prompt,
            source="async_delegation_wakeup",
            turn_key_override=turn_key,
            user_message_metadata={
                "_hermes_message_class": "context_anchor",
                "_hermes_scaffold_kind": "async_delegation_completion",
            },
            server_turn_metadata={
                "delegation_id": delegation_id,
                "origin_turn_key": turn_key,
            },
        )
    except Exception as exc:
        result = {"_status": 500, "error": str(exc)}
    status = int((result or {}).get("_status", 200) or 200)
    if status == 409:
        if (result or {}).get("error") == "process_wakeup_paused":
            with _get_session_agent_lock(sid):
                session = _load_session(sid)
                if session is not None:
                    mark_async_delegation_wakeup(
                        session,
                        delegation_id,
                        wakeup_state="queued",
                        content="",
                        error_code="provider_paused",
                    )
            notify(sid, delay=60.0)
        elif (result or {}).get("error") == "会话已归档，暂不启动后台委派唤醒":
            notify(sid, delay=60.0)
        else:
            notify(sid, delay=1.0)
    elif status >= 500:
        from integration.async_delegation_turns import record_retryable_wakeup_start_failure

        with _get_session_agent_lock(sid):
            session = _load_session(sid)
            record = (
                record_retryable_wakeup_start_failure(
                    session,
                    delegation_id,
                    previous_attempts=previous_attempts,
                    error_code="start_retryable_failure",
                )
                if session is not None
                else None
            )
        if record is not None and record.get("wakeup_state") == "queued":
            attempts = max(1, int((record.get("wakeup") or {}).get("start_attempts") or 1))
            notify(sid, delay=float(min(16, 2 ** (attempts - 1))))
    elif status >= 400:
        from integration.async_delegation_turns import settle_wakeup

        with _get_session_agent_lock(sid):
            session = _load_session(sid)
            if session is not None:
                settle_wakeup(
                    session,
                    delegation_id,
                    stream_id=None,
                    generation=None,
                    status="failed",
                    error_code=f"start_http_{status}",
                    clear_prompt=False,
                )


def _safety_sweep() -> None:
    try:
        from api.models import Session
        from api.config import _get_session_agent_lock

        # The durable sidecar is scanned at startup; steady-state sweeps only
        # revisit in-memory candidates so polling cost stays bounded.
        with _LOCK:
            candidate_ids = list(_TICKETS)
        for sid in candidate_ids:
            with _get_session_agent_lock(sid):
                session = Session.load(sid)
                if session is None:
                    continue
                _reconcile_ack_pending(session)
                from integration.async_delegation_turns import select_next_queued_wakeup

                if select_next_queued_wakeup(session) is not None:
                    notify(sid)
                elif _has_ack_pending(session):
                    notify(sid, delay=_ACK_RETRY_SECONDS)
    except Exception:
        logger.warning("async delegation inbox safety sweep failed", exc_info=True)


def _loop() -> None:
    next_sweep = time.monotonic() + _SAFETY_SWEEP_SECONDS
    while not _STOP.is_set():
        sid = None
        with _WAKE:
            now = time.monotonic()
            if now >= next_sweep:
                next_sweep = now + _SAFETY_SWEEP_SECONDS
                sid = ""
            else:
                sid = _pop_due()
                if sid is None:
                    timeout = max(0.1, min(next_sweep - now, 5.0))
                    _WAKE.wait(timeout)
                    continue
        if sid == "":
            _safety_sweep()
            continue
        try:
            _drain_one(sid)
        except Exception:
            logger.warning("async delegation inbox drain failed for session %s", sid, exc_info=True)
            notify(sid, delay=1.0)


def start() -> bool:
    global _THREAD
    with _WAKE:
        if _THREAD is not None and _THREAD.is_alive():
            return False
        _STOP.clear()
        _THREAD = threading.Thread(target=_loop, name="hermes-async-delegation-inbox", daemon=True)
        _THREAD.start()
        _WAKE.notify()
        return True


def stop(timeout: float = 2.0) -> None:
    _STOP.set()
    with _WAKE:
        _WAKE.notify_all()
    thread = _THREAD
    if thread is not None and thread.is_alive():
        thread.join(timeout=max(0.0, float(timeout)))


def recover() -> int:
    """Raw startup scan; callers must invoke after sidecar restore and before drain."""
    count = 0
    try:
        from api.models import Session
        from api.config import _get_session_agent_lock
        from integration.async_delegation_turns import (
            reconcile_async_wakeup_before_stale_cleanup,
            select_next_queued_wakeup,
        )

        for sid in _sidecar_session_ids():
            with _get_session_agent_lock(sid):
                session = Session.load(sid)
                if session is None:
                    continue
                _reconcile_ack_pending(session)
                records = getattr(session, "async_delegation_origins", None)
                running_streams = []
                if isinstance(records, dict):
                    for raw in records.values():
                        if not isinstance(raw, dict) or raw.get("wakeup_state") != "running":
                            continue
                        wakeup = raw.get("wakeup") if isinstance(raw.get("wakeup"), dict) else {}
                        stream_id = str(wakeup.get("stream_id") or "").strip()
                        if stream_id:
                            running_streams.append(stream_id)
                recovered_running = False
                for stream_id in running_streams:
                    recovered_running = (
                        reconcile_async_wakeup_before_stale_cleanup(
                            session,
                            stream_id=stream_id,
                            require_pending_source=False,
                        )
                        or recovered_running
                    )
                if recovered_running:
                    session.save(touch_updated_at=False)
                if select_next_queued_wakeup(session) is not None:
                    notify(sid)
                    count += 1
                elif _has_ack_pending(session):
                    notify(sid, delay=_ACK_RETRY_SECONDS)
                    count += 1
    except Exception:
        logger.warning("async delegation inbox recovery failed", exc_info=True)
    if count:
        logger.info("async_delegation_scheduler_recovered queued_sessions=%s", count)
    return count

"""Structured diagnostics for chat streaming.

This module records human-readable server-side console logs and a short-lived
in-memory summary keyed by ``stream_id``. Diagnostic events are never sent over
the chat SSE channel.
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from integration.request_logging.formatting import with_timestamp
from integration.request_logging.logger import console_error, console_info, console_warning

STREAM_DIAG_SLOW_MS = 1000.0
_STREAM_DIAG_PREFIX = "[stream_diag]"
_SUMMARY_TTL_SECONDS = 15 * 60
_SUMMARIES: dict[str, dict[str, Any]] = {}
_SUMMARIES_LOCK = threading.Lock()

_TRUTHY = {"1", "true", "yes", "on", "debug"}
_CORE_CONSOLE_FIELDS = (
    "event",
    "stream_id",
    "session_id",
    "model",
    "provider",
    "profile",
    "elapsed_ms",
    "duration_ms",
    "wait_ms",
    "total_ms",
    "cache_hit",
    "constructor_total_ms",
    "agent_init_total_ms",
    "run_conversation_ms",
    "first_token_ms",
    "final_save_ms",
    "error_type",
    "error",
)
_DEBUG_CONSOLE_FIELDS = (
    "request_id",
    "workspace_hash",
    "agent_source_file",
    "event_callback_supported",
    "event_callback_passed",
    "event_callback_will_be_passed",
    "agent_init_breakdown_available",
    "agent_init_missing_reason",
    "toolset_count",
    "fallback_count",
    "event_counts",
)
_FIELD_ALIASES = {
    "stream_id": "stream",
    "session_id": "session",
    "elapsed_ms": "elapsed",
    "duration_ms": "duration",
    "wait_ms": "wait",
    "total_ms": "total",
    "constructor_total_ms": "constructor",
    "agent_init_total_ms": "agent_init",
    "run_conversation_ms": "run",
    "first_token_ms": "first_token",
    "final_save_ms": "save",
    "cache_elapsed_ms": "cache",
    "event_callback_supported": "callback_supported",
    "event_callback_passed": "callback_passed",
    "event_callback_will_be_passed": "callback_will_pass",
    "toolset_count": "tools",
    "fallback_count": "fallback",
    "agent_source_file": "source",
}
_ID_FIELDS = {"stream_id", "session_id", "request_id"}
_MS_FIELDS = {
    "elapsed_ms",
    "duration_ms",
    "wait_ms",
    "total_ms",
    "constructor_total_ms",
    "agent_init_total_ms",
    "run_conversation_ms",
    "first_token_ms",
    "final_save_ms",
    "cache_elapsed_ms",
}


def mode() -> str:
    raw = str(os.getenv("HERMES_WEBUI_STREAM_DIAG", "") or "").strip().lower()
    if raw == "debug":
        return "debug"
    if raw in _TRUTHY:
        return "1"
    return "0"


def enabled() -> bool:
    return mode() != "0"


def debug_enabled() -> bool:
    return mode() == "debug"


def monotonic_ms() -> float:
    return time.monotonic() * 1000.0


def elapsed_ms(start_ms: float | None) -> float:
    if start_ms is None:
        return 0.0
    return round(max(0.0, monotonic_ms() - start_ms), 1)


def safe_workspace_hash(workspace: Any) -> str:
    value = str(workspace or "").strip()
    if not value:
        return ""
    try:
        value = str(Path(value).expanduser().resolve())
    except Exception:
        pass
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:12]


def safe_count(value: Any) -> int:
    try:
        return len(value) if value is not None else 0
    except Exception:
        return 0


def _clean_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) > 240:
            return value[:240] + "..."
        return value
    if isinstance(value, (list, tuple, set)):
        return [_clean_value(v) for v in list(value)[:20]]
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in list(value.items())[:40]:
            key_s = str(key)
            key_l = key_s.lower()
            if any(token in key_l for token in ("key", "token", "secret", "password", "cookie", "authorization")):
                cleaned[key_s] = "[redacted]"
            else:
                cleaned[key_s] = _clean_value(item)
        return cleaned
    return str(value)[:240]


def _event_side(event: str) -> str:
    event_name = str(event or "")
    if event_name.startswith("webui."):
        return "webui"
    if event_name.startswith("agent.") or event_name.startswith("agent_"):
        return "agent"
    return "unknown"


def _base_payload(event: str, message_zh: str, fields: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "event": event,
        "side": _event_side(event),
        "message_zh": message_zh,
        "ts": round(time.time(), 3),
    }
    for k, v in fields.items():
        if v is None:
            continue
        key_l = str(k).lower()
        if any(token in key_l for token in ("key", "token", "secret", "password", "cookie", "authorization")):
            payload[k] = "[redacted]"
        else:
            payload[k] = _clean_value(v)
    elapsed = payload.get("elapsed_ms", payload.get("duration_ms", payload.get("wait_ms", payload.get("total_ms"))))
    if isinstance(elapsed, (int, float)):
        payload["slow"] = float(elapsed) >= STREAM_DIAG_SLOW_MS
    return payload


def _elapsed_for_status(payload: dict[str, Any]) -> float | None:
    for key in ("elapsed_ms", "duration_ms", "wait_ms", "total_ms"):
        value = payload.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return None


def _format_value(key: str, value: Any) -> str:
    if key in _ID_FIELDS:
        return str(value)[:8]
    if key in _MS_FIELDS and isinstance(value, (int, float)):
        return f"{value}ms"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _format_console_line(payload: dict[str, Any]) -> str:
    side = str(payload.get("side") or "unknown")
    elapsed = _elapsed_for_status(payload)
    if payload.get("error_type") or payload.get("error"):
        status = "ERROR"
    elif payload.get("slow"):
        status = f"SLOW {elapsed}ms" if elapsed is not None else "SLOW"
    elif elapsed is not None:
        status = f"OK {elapsed}ms"
    else:
        status = "OK"

    details = []
    fields = list(_CORE_CONSOLE_FIELDS)
    if debug_enabled():
        fields.extend(_DEBUG_CONSOLE_FIELDS)
    for key in fields:
        if key in {"elapsed_ms", "duration_ms", "wait_ms", "total_ms"}:
            continue
        if key not in payload:
            continue
        value = payload.get(key)
        if value is None:
            continue
        label = _FIELD_ALIASES.get(key, key)
        details.append(f"{label}={_format_value(key, value)}")
    detail_text = " ".join(details)
    suffix = f" | {detail_text}" if detail_text else ""
    line = f"{_STREAM_DIAG_PREFIX}[{side}][{status}] {payload.get('message_zh') or payload.get('event')}{suffix}"
    return with_timestamp(line, payload)


def log_event(event: str, message_zh: str, **fields: Any) -> dict[str, Any]:
    """Log one diagnostic event and return the sanitized payload."""
    payload = _base_payload(event, message_zh, fields)
    if not enabled():
        return payload
    line = _format_console_line(payload)
    if payload.get("error_type") or payload.get("error"):
        console_error(line)
    elif payload.get("slow"):
        console_warning(line)
    else:
        console_info(line)
    return payload


def _prune_locked(now: float | None = None) -> None:
    now = time.time() if now is None else now
    stale = [sid for sid, data in _SUMMARIES.items() if now - float(data.get("updated_at", now)) > _SUMMARY_TTL_SECONDS]
    for sid in stale:
        _SUMMARIES.pop(sid, None)


def update_stream_summary(stream_id: str, **fields: Any) -> dict[str, Any]:
    if not stream_id:
        return {}
    with _SUMMARIES_LOCK:
        _prune_locked()
        data = _SUMMARIES.setdefault(stream_id, {"stream_id": stream_id, "created_at": time.time()})
        data.update({k: _clean_value(v) for k, v in fields.items() if v is not None})
        data["updated_at"] = time.time()
        return dict(data)


def get_stream_summary(stream_id: str) -> dict[str, Any]:
    if not stream_id:
        return {}
    with _SUMMARIES_LOCK:
        data = _SUMMARIES.get(stream_id) or {}
        return dict(data)


def clear_stream_summary(stream_id: str) -> None:
    if not stream_id:
        return
    with _SUMMARIES_LOCK:
        _SUMMARIES.pop(stream_id, None)


class StreamDiag:
    """Per-stream helper for timings and aggregate counters."""

    def __init__(self, *, stream_id: str = "", session_id: str = "", request_id: str = "", profile: str = "", model: str = "", provider: str = "", workspace: Any = None) -> None:
        self.stream_id = str(stream_id or "")
        self.session_id = str(session_id or "")
        self.request_id = str(request_id or "")
        self.profile = str(profile or "")
        self.model = str(model or "")
        self.provider = str(provider or "")
        self.workspace_hash = safe_workspace_hash(workspace)
        self.start_ms = monotonic_ms()
        self.event_counts: Counter[str] = Counter()
        self.queue_failures = 0
        self.journal_failures = 0
        self.events_enqueued = 0
        self.first_event_ms: float | None = None
        self.first_token_ms: float | None = None
        self.first_reasoning_ms: float | None = None
        self.first_tool_ms: float | None = None
        self.last_event_type = ""

    def fields(self, **extra: Any) -> dict[str, Any]:
        base = {
            "stream_id": self.stream_id,
            "session_id": self.session_id,
            "request_id": self.request_id,
            "profile": self.profile,
            "model": self.model,
            "provider": self.provider,
            "workspace_hash": self.workspace_hash,
        }
        base.update(extra)
        return base

    def event(self, event: str, message_zh: str, **fields: Any) -> dict[str, Any]:
        payload = log_event(event, message_zh, **self.fields(**fields))
        if self.stream_id:
            update_stream_summary(self.stream_id, **{event.replace(".", "_") + "_ms": payload.get("elapsed_ms")})
        return payload

    @contextmanager
    def stage(self, event: str, message_zh: str, **fields: Any) -> Iterator[dict[str, Any]]:
        started = monotonic_ms()
        details: dict[str, Any] = {}
        try:
            yield details
        finally:
            merged = dict(fields)
            merged.update(details)
            merged.setdefault("elapsed_ms", elapsed_ms(started))
            self.event(event, message_zh, **merged)

    def note_queued_event(self, event_type: str) -> None:
        self.events_enqueued += 1
        self.event_counts[str(event_type or "unknown")] += 1
        self.last_event_type = str(event_type or "")
        now_elapsed = elapsed_ms(self.start_ms)
        if self.first_event_ms is None:
            self.first_event_ms = now_elapsed
        if event_type == "token" and self.first_token_ms is None:
            self.first_token_ms = now_elapsed
        elif event_type == "reasoning" and self.first_reasoning_ms is None:
            self.first_reasoning_ms = now_elapsed
        elif str(event_type or "").startswith("tool") and self.first_tool_ms is None:
            self.first_tool_ms = now_elapsed

    def summary_fields(self) -> dict[str, Any]:
        return self.fields(
            total_ms=elapsed_ms(self.start_ms),
            events_enqueued=self.events_enqueued,
            event_counts=dict(self.event_counts),
            first_event_ms=self.first_event_ms,
            first_token_ms=self.first_token_ms,
            first_reasoning_ms=self.first_reasoning_ms,
            first_tool_ms=self.first_tool_ms,
            last_event_type=self.last_event_type,
            queue_failures=self.queue_failures,
            journal_failures=self.journal_failures,
        )

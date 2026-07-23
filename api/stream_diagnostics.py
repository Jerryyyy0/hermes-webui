"""Structured diagnostics for chat streaming.

This module records human-readable server-side console logs and a short-lived
in-memory summary keyed by ``stream_id``. Diagnostic events are never sent over
the chat SSE channel.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from integration.project_logging import is_debug_level
from integration.project_logging import console_error, console_info, console_warning, with_timestamp

STREAM_DIAG_SLOW_MS = 1000.0
_STREAM_DIAG_PREFIX = "[stream_diag]"
_SUMMARY_TTL_SECONDS = 15 * 60
_SUMMARIES: dict[str, dict[str, Any]] = {}
_SUMMARIES_LOCK = threading.Lock()

_TRUTHY = {"1", "true", "yes", "on", "debug"}  # legacy tests may reference mode values
_CORE_CONSOLE_FIELDS = (
    "event",
    "phase",
    "step",
    "phase_name",
    "outcome",
    "stream_id",
    "session_id",
    "model",
    "provider",
    "profile",
    "elapsed_ms",
    "duration_ms",
    "wait_ms",
    "hold_ms",
    "total_ms",
    "cache_hit",
    "constructor_total_ms",
    "agent_init_total_ms",
    "run_conversation_ms",
    "first_token_ms",
    "first_visible_token_ms",
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
    "first_visible_token_ms": "first_visible_token",
    "final_save_ms": "save",
    "cache_elapsed_ms": "cache",
    "phase": "phase",
    "step": "step",
    "phase_name": "phase_name",
    "outcome": "outcome",
    "hold_ms": "hold",
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
    "hold_ms",
    "total_ms",
    "constructor_total_ms",
    "agent_init_total_ms",
    "run_conversation_ms",
    "first_token_ms",
    "first_visible_token_ms",
    "final_save_ms",
    "cache_elapsed_ms",
}

# Stable diagnostic hierarchy. New steps may be appended but existing identifiers
# must not be repurposed: operators use them to compare latency across releases.
_EVENT_PHASES = {
    "webui.chat_start.accepted": ("P0", "S0.1", "chat-start validation"),
    "webui.chat_start.stream_created": ("P0", "S0.2", "stream creation"),
    "webui.chat_start.worker_dispatched": ("P0", "S0.3", "worker dispatch"),
    "webui.worker.start": ("P2", "BEGIN", "worker pre-agent setup"),
    "webui.worker.pre_agent_journal": ("P2", "S2.1", "journal and live-stream state"),
    "webui.worker.profile_context": ("P2", "S2.2", "session and profile resolution"),
    "webui.worker.profile_runtime": ("P2", "S2.3", "profile runtime and skill prewarm"),
    "webui.worker.process_env": ("P2", "S2.4", "process environment lock"),
    "webui.worker.mcp_discovery": ("P2", "S2.5", "MCP discovery"),
    "webui.worker.callbacks_registered": ("P2", "S2.6", "approval and clarify callbacks"),
    "webui.worker.pre_agent_setup": ("P2", "SUMMARY", "worker pre-agent setup total"),
    "webui.worker.agent_import": ("P3", "S3.0.1", "agent class load and capability check"),
    "webui.worker.agent_kwargs_ready": ("P3", "S3.0.2", "agent construction arguments"),
    "webui.worker.agent_cache_lookup_start": ("P3", "S3.1", "agent cache lookup"),
    "webui.worker.agent_cache_lookup_done": ("P3", "S3.1", "agent cache lookup"),
    "webui.worker.agent_construct_start": ("P3", "S3.2", "agent construction"),
    "webui.worker.agent_construct_done": ("P3", "S3.2", "agent construction"),
    "agent_init.basic": ("P3", "S3.2.1", "agent basic initialization"),
    "agent_init.provider_client": ("P3", "S3.2.2", "provider client initialization"),
    "agent_init.tools_registry": ("P3", "S3.2.3", "tool registry initialization"),
    "agent_init.memory_provider": ("P3", "S3.2.4", "memory provider initialization"),
    "agent_init.config_model_metadata": ("P3", "S3.2.5", "model metadata initialization"),
    "agent_init.context_engine": ("P3", "S3.2.6", "context engine initialization"),
    "webui.worker.agent_ready": ("P3", "SUMMARY", "agent preparation total"),
    "webui.worker.context_prepared": ("P4", "S4.1", "context preparation"),
    "agent.model_first_delta": ("P4", "S4.2", "model request to first delta"),
    "webui.stream.first_visible_token": ("P4", "S4.3", "first delta to visible token"),
    "webui.worker.run_conversation": ("P4", "SUMMARY", "model execution total"),
    "webui.worker.finalize": ("P5", "S5.1", "finalize and persist"),
    "webui.worker.cleanup_summary": ("P5", "SUMMARY", "worker total"),
    "webui.stream.open": ("P1", "S1.1", "SSE connection"),
    "webui.stream.resolve": ("P1", "S1.2", "SSE source resolution"),
    "webui.stream.first_event": ("P1", "S1.3", "first SSE event"),
    "webui.stream.summary": ("P6", "SUMMARY", "SSE lifecycle total"),
}

_AGENT_INIT_PHASE = ("P3", "S3.2", "agent initialization")


def mode() -> str:
    if not enabled():
        return "0"
    if is_debug_level():
        return "debug"
    return "1"


def enabled() -> bool:
    from integration.project_logging.config import resolve_log_level

    return resolve_log_level() <= logging.INFO


def debug_enabled() -> bool:
    return is_debug_level()


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


def _phase_fields(event: str, fields: dict[str, Any]) -> dict[str, Any]:
    resolved = dict(fields)
    phase = _EVENT_PHASES.get(event)
    if phase is None and str(event).startswith("agent_init."):
        phase = _AGENT_INIT_PHASE
    if phase is None:
        return resolved
    for key, value in zip(("phase", "step", "phase_name"), phase):
        resolved.setdefault(key, value)
    return resolved


def _base_payload(event: str, message_zh: str, fields: dict[str, Any]) -> dict[str, Any]:
    fields = _phase_fields(event, fields)
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
        if k not in _MS_FIELDS and any(token in key_l for token in ("key", "token", "secret", "password", "cookie", "authorization")):
            payload[k] = "[redacted]"
        else:
            payload[k] = _clean_value(v)
    elapsed = payload.get("duration_ms", payload.get("elapsed_ms", payload.get("wait_ms", payload.get("total_ms"))))
    if isinstance(elapsed, (int, float)):
        payload["slow"] = float(elapsed) >= STREAM_DIAG_SLOW_MS
    return payload


def _elapsed_for_status(payload: dict[str, Any]) -> float | None:
    for key in ("duration_ms", "elapsed_ms", "wait_ms", "total_ms"):
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
        if key not in payload:
            continue
        value = payload.get(key)
        if value is None:
            continue
        label = _FIELD_ALIASES.get(key, key)
        details.append(f"{label}={_format_value(key, value)}")
    detail_text = " ".join(details)
    suffix = f" | {detail_text}" if detail_text else ""
    phase_marker = " ".join(
        str(payload[key])
        for key in ("phase", "step")
        if payload.get(key)
    )
    phase_prefix = f"[{phase_marker}]" if phase_marker else ""
    line = f"{_STREAM_DIAG_PREFIX}[{side}]{phase_prefix}[{status}] {payload.get('message_zh') or payload.get('event')}{suffix}"
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
        self.first_visible_token_ms: float | None = None
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
            event_key = event.replace(".", "_") + "_ms"
            summary_fields = {event_key: payload.get("duration_ms", payload.get("elapsed_ms"))}
            step = str(payload.get("step") or "")
            phase = str(payload.get("phase") or "")
            if step:
                existing = get_stream_summary(self.stream_id).get("stage_timings") or {}
                stage_timings = dict(existing) if isinstance(existing, dict) else {}
                stage_key = f"{phase}.{step}" if phase else step
                stage_timings[stage_key] = {
                    "phase": phase,
                    "name": payload.get("phase_name"),
                    "duration_ms": payload.get("duration_ms"),
                    "elapsed_ms": payload.get("elapsed_ms"),
                    "outcome": payload.get("outcome", "ok"),
                }
                summary_fields["stage_timings"] = stage_timings
                summary_fields["last_stage"] = stage_key
            update_stream_summary(self.stream_id, **summary_fields)
        return payload

    @contextmanager
    def stage(self, event: str, message_zh: str, **fields: Any) -> Iterator[dict[str, Any]]:
        started = monotonic_ms()
        details: dict[str, Any] = {}
        try:
            yield details
        except Exception as exc:
            details.setdefault("outcome", "error")
            details.setdefault("error_type", type(exc).__name__)
            raise
        finally:
            merged = dict(fields)
            merged.update(details)
            merged.setdefault("duration_ms", elapsed_ms(started))
            merged.setdefault("elapsed_ms", elapsed_ms(self.start_ms))
            merged.setdefault("outcome", "ok")
            self.event(event, message_zh, **merged)

    def note_model_first_delta(self) -> float:
        if self.first_token_ms is None:
            self.first_token_ms = elapsed_ms(self.start_ms)
        return self.first_token_ms

    def note_queued_event(self, event_type: str) -> None:
        self.events_enqueued += 1
        self.event_counts[str(event_type or "unknown")] += 1
        self.last_event_type = str(event_type or "")
        now_elapsed = elapsed_ms(self.start_ms)
        if self.first_event_ms is None:
            self.first_event_ms = now_elapsed
        if event_type == "token" and self.first_visible_token_ms is None:
            self.first_visible_token_ms = now_elapsed
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
            first_visible_token_ms=self.first_visible_token_ms,
            first_reasoning_ms=self.first_reasoning_ms,
            first_tool_ms=self.first_tool_ms,
            last_event_type=self.last_event_type,
            queue_failures=self.queue_failures,
            journal_failures=self.journal_failures,
        )

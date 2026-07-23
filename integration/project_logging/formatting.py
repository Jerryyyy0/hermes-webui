"""Shared formatting and redaction helpers for WebUI logging."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Mapping

_MAX_VALUE_LEN = 240

_SENSITIVE_KEY_RE = re.compile(
    r"(password|passwd|secret|token|api[_-]?key|authorization|cookie|bearer|session_key|access_token|refresh_token)",
    re.IGNORECASE,
)


def format_timestamp(ts: Any = None) -> str:
    """Return a readable local timestamp with millisecond precision."""
    dt: datetime
    if ts is None:
        dt = datetime.now().astimezone()
    elif isinstance(ts, (int, float)):
        dt = datetime.fromtimestamp(float(ts)).astimezone()
    else:
        raw = str(ts).strip()
        try:
            dt = datetime.fromtimestamp(float(raw)).astimezone()
        except (TypeError, ValueError, OSError):
            try:
                dt = datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone()
            except (TypeError, ValueError):
                return one_line(raw)
    return f"{dt:%Y-%m-%d %H:%M:%S}.{dt.microsecond // 1000:03d}"


def one_line(value: Any, max_len: int = _MAX_VALUE_LEN) -> str:
    if value is None:
        return "-"
    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    text = " ".join(text.split())
    if not text:
        return "-"
    if max_len > 0 and len(text) > max_len:
        return text[: max_len - 1] + "…" if max_len > 1 else "…"
    return text


def short_id(value: Any, size: int = 8) -> str:
    text = one_line(value, max_len=0)
    return text[: max(size, 1)] if text != "-" else text


def _quote_if_needed(value: str) -> str:
    if value == "-":
        return value
    if any(ch.isspace() for ch in value) or any(ch in value for ch in '"|'):
        escaped = value.replace('"', '\\"')
        return f'"{escaped}"'
    return value


def is_sensitive_key(key: str) -> bool:
    return bool(_SENSITIVE_KEY_RE.search(str(key or "")))


def sanitize_fields(fields: Mapping[str, Any] | None) -> dict[str, Any]:
    """Drop or redact sensitive keys before logging."""
    if not fields:
        return {}
    safe: dict[str, Any] = {}
    for key, value in fields.items():
        if value is None or value == "":
            continue
        if is_sensitive_key(key):
            safe[key] = "<redacted>"
            continue
        safe[key] = value
    return safe


def format_kv(fields: Mapping[str, Any], aliases: Mapping[str, str] | None = None) -> str:
    aliases = aliases or {}
    parts: list[str] = []
    for key, value in sanitize_fields(fields).items():
        label = aliases.get(key, key)
        text = one_line(value)
        if text == "-":
            continue
        parts.append(f"{label}={_quote_if_needed(text)}")
    return " ".join(parts)


def with_timestamp(line: str, record: Mapping[str, Any] | None = None) -> str:
    ts = record.get("ts") if record else None
    return f"{format_timestamp(ts)} {line}"


def format_event_line(
    *,
    prefix: str,
    event: str | None = None,
    fields: Mapping[str, Any] | None = None,
    ts: Any = None,
) -> str:
    """Format a grep-friendly event line with optional timestamp prefix."""
    parts = [prefix]
    if event:
        parts.append(f"event={one_line(event, max_len=120)}")
    kv = format_kv(fields or {})
    if kv:
        parts.append(kv)
    line = " ".join(parts)
    if ts is not None:
        return with_timestamp(line, {"ts": ts})
    return with_timestamp(line)


def _record_status(record: Mapping[str, Any]) -> str:
    return one_line(record.get("status"))


def _record_method(record: Mapping[str, Any]) -> str:
    return one_line(record.get("method"))


def _record_path(record: Mapping[str, Any]) -> str:
    return one_line(record.get("path"))


def format_request_line(record: Mapping[str, Any]) -> str:
    """Format an HTTP access log record for the console."""
    ms = record.get("ms")
    try:
        ms_text = f"{float(ms):.1f}ms"
    except (TypeError, ValueError):
        ms_text = one_line(ms)

    extras = format_kv(
        {
            "remote": record.get("remote"),
            "forwarded_for": record.get("forwarded_for"),
            "request_id": short_id(record.get("request_id")) if record.get("request_id") else None,
            "error": record.get("error_summary"),
        }
    )
    suffix = f" {extras}" if extras else ""
    line = f"{_record_method(record)} {_record_path(record)} -> {_record_status(record)} {ms_text}{suffix}"
    return with_timestamp(line, record)


def format_api_error_line(record: Mapping[str, Any]) -> str:
    """Format an API error record for the console."""
    extras = format_kv(
        {
            "source": record.get("source"),
            "remote": record.get("remote"),
            "forwarded_for": record.get("forwarded_for"),
            "request_id": short_id(record.get("request_id")) if record.get("request_id") else None,
            "error": record.get("error"),
            "message": record.get("message"),
            "traceback": "yes" if record.get("traceback") else None,
        }
    )
    suffix = f" {extras}" if extras else ""
    line = f"[webui][api_error] {_record_method(record)} {_record_path(record)} -> {_record_status(record)}{suffix}"
    return with_timestamp(line, record)


def _format_stages(stages: Any) -> str | None:
    if not isinstance(stages, list):
        return None
    parts: list[str] = []
    for stage in stages:
        if not isinstance(stage, Mapping):
            continue
        name = one_line(stage.get("name"), max_len=80)
        if name == "-":
            continue
        ms = stage.get("ms")
        try:
            ms_text = f"{float(ms):.1f}ms"
        except (TypeError, ValueError):
            ms_text = one_line(ms)
        parts.append(f"{name}:{ms_text}")
    return ",".join(parts) if parts else None


def format_slow_request_line(record: Mapping[str, Any], state: str) -> str:
    """Format a slow-request diagnostic record for the console."""
    elapsed = record.get("elapsed_ms")
    try:
        elapsed_text = f"{float(elapsed):.1f}ms"
    except (TypeError, ValueError):
        elapsed_text = one_line(elapsed)

    extras = format_kv(
        {
            "elapsed": elapsed_text,
            "stages": _format_stages(record.get("stages")),
            "current_stage": record.get("current_stage"),
            "request_id": short_id(record.get("request_id")),
            "thread_stacks": "yes" if record.get("thread_stacks") else None,
        }
    )
    suffix = f" {extras}" if extras else ""
    line = f"[webui][slow_request][{one_line(state)}] {_record_method(record)} {_record_path(record)}{suffix}"
    return with_timestamp(line, record)

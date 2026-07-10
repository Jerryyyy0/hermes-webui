"""Human-readable console formatting for WebUI request logs."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

_MAX_VALUE_LEN = 240


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


def with_timestamp(line: str, record: Mapping[str, Any] | None = None) -> str:
    """Prefix a pretty console line with a readable timestamp."""
    ts = record.get("ts") if record else None
    return f"{format_timestamp(ts)} {line}"


def short_id(value: Any, size: int = 8) -> str:
    """Return a compact identifier prefix for long values."""
    text = one_line(value, max_len=0)
    return text[: max(size, 1)] if text != "-" else text


def one_line(value: Any, max_len: int = _MAX_VALUE_LEN) -> str:
    """Convert a value to safe single-line text for console output."""
    if value is None:
        return "-"
    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    text = " ".join(text.split())
    if not text:
        return "-"
    if max_len > 0 and len(text) > max_len:
        return text[: max_len - 1] + "…" if max_len > 1 else "…"
    return text


def _quote_if_needed(value: str) -> str:
    if value == "-":
        return value
    if any(ch.isspace() for ch in value) or any(ch in value for ch in '"|'):
        escaped = value.replace('"', '\\"')
        return f'"{escaped}"'
    return value


def format_kv(fields: Mapping[str, Any], aliases: Mapping[str, str] | None = None) -> str:
    """Format present fields as grep-friendly key=value tokens."""
    aliases = aliases or {}
    parts: list[str] = []
    for key, value in fields.items():
        if value is None or value == "":
            continue
        label = aliases.get(key, key)
        text = one_line(value)
        if text == "-":
            continue
        parts.append(f"{label}={_quote_if_needed(text)}")
    return " ".join(parts)


def _status(record: Mapping[str, Any]) -> str:
    return one_line(record.get("status"))


def _method(record: Mapping[str, Any]) -> str:
    return one_line(record.get("method"))


def _path(record: Mapping[str, Any]) -> str:
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
            "error": record.get("error_summary"),
        }
    )
    suffix = f" {extras}" if extras else ""
    line = f"[webui][request] {_method(record)} {_path(record)} -> {_status(record)} {ms_text}{suffix}"
    return with_timestamp(line, record)


def format_api_error_line(record: Mapping[str, Any]) -> str:
    """Format an API error record for the console."""
    extras = format_kv(
        {
            "source": record.get("source"),
            "remote": record.get("remote"),
            "forwarded_for": record.get("forwarded_for"),
            "error": record.get("error"),
            "message": record.get("message"),
            "traceback": "yes" if record.get("traceback") else None,
        }
    )
    suffix = f" {extras}" if extras else ""
    line = f"[webui][api_error] {_method(record)} {_path(record)} -> {_status(record)}{suffix}"
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
    line = f"[webui][slow_request][{one_line(state)}] {_method(record)} {_path(record)}{suffix}"
    return with_timestamp(line, record)

"""Browser preview SSE helpers for remote Camofox/VNC URLs."""
from __future__ import annotations

import os
import threading
from typing import Callable
from urllib.parse import urlparse

BROWSER_PREVIEW_EVENT = "browser_preview"
BROWSER_PREVIEW_SOURCE = "camofox"
BROWSER_PREVIEW_URL_ENV = "BROWSER_PREVIEW_URL"
BROWSER_PREVIEW_DELAY_ENV = "BROWSER_PREVIEW_DELAY_SECONDS"
BROWSER_PREVIEW_DEFAULT_DELAY_SECONDS = 5.0


def is_browser_tool_name(name: str) -> bool:
    return str(name or "").strip().startswith("browser_")


def _normalize_http_url(raw: str) -> str:
    raw = str(raw or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""
    url = parsed.geturl()
    if "?" not in url and "#" not in url:
        url = url.rstrip("/")
    return url


def resolve_browser_preview_url(environ: dict[str, str] | None = None) -> str:
    """Preview URL for workspace iframe from BROWSER_PREVIEW_URL."""
    source = os.environ if environ is None else environ
    return _normalize_http_url(source.get(BROWSER_PREVIEW_URL_ENV) or "")


def resolve_browser_preview_frame_origin(environ: dict[str, str] | None = None) -> str:
    """Origin (scheme + host[:port]) allowed in CSP frame-src for the preview URL."""
    url = resolve_browser_preview_url(environ)
    if not url:
        return ""
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def resolve_camofox_preview_url(environ: dict[str, str] | None = None) -> str:
    return resolve_browser_preview_url(environ)


def resolve_camofox_frame_origin(environ: dict[str, str] | None = None) -> str:
    return resolve_browser_preview_frame_origin(environ)


def preview_delay_seconds(environ: dict[str, str] | None = None) -> float:
    """Seconds to wait before emitting browser_preview SSE (0 = immediate)."""
    if environ is not None and BROWSER_PREVIEW_DELAY_ENV in environ:
        raw = str(environ.get(BROWSER_PREVIEW_DELAY_ENV, "")).strip()
    else:
        raw = str(os.environ.get(BROWSER_PREVIEW_DELAY_ENV, BROWSER_PREVIEW_DEFAULT_DELAY_SECONDS)).strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return BROWSER_PREVIEW_DEFAULT_DELAY_SECONDS


def browser_preview_payload(
    session_id: str,
    stream_id: str,
    tool_name: str,
    environ: dict[str, str] | None = None,
) -> dict | None:
    url = resolve_browser_preview_url(environ)
    if not url:
        return None
    return {
        "session_id": str(session_id or ""),
        "stream_id": str(stream_id or ""),
        "url": url,
        "source": BROWSER_PREVIEW_SOURCE,
        "tool": str(tool_name or "").strip(),
    }


class BrowserPreviewEmitter:
    """Emit at most one browser_preview SSE event per stream worker."""

    def __init__(self) -> None:
        self._emitted = False
        self._timer: threading.Timer | None = None

    def maybe_emit(
        self,
        put_fn: Callable[[str, dict], None],
        session_id: str,
        stream_id: str,
        tool_name: str,
        environ: dict[str, str] | None = None,
    ) -> bool:
        if self._emitted:
            return False
        if not is_browser_tool_name(tool_name):
            return False
        payload = browser_preview_payload(session_id, stream_id, tool_name, environ)
        if not payload:
            return False
        self._emitted = True
        delay = preview_delay_seconds(environ)

        def _emit() -> None:
            put_fn(BROWSER_PREVIEW_EVENT, payload)

        if delay <= 0:
            _emit()
        else:
            self._timer = threading.Timer(delay, _emit)
            self._timer.daemon = True
            self._timer.start()
        return True

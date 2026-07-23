"""Central logging configuration for Hermes WebUI."""

from __future__ import annotations

import logging
import os
import sys
import threading
from typing import TextIO

_ROOT_LOGGER_NAME = "hermes.webui"
_CONFIGURED_ATTR = "_hermes_webui_project_logging_configured"
_CONFIGURE_LOCK = threading.RLock()

_THIRD_PARTY_LOGGERS: dict[str, int] = {
    "urllib3": logging.WARNING,
    "httpx": logging.WARNING,
    "httpcore": logging.WARNING,
    "asyncio": logging.WARNING,
}


class _CurrentStderrHandler(logging.Handler):
    """Write log records to the configured or current stderr stream at emit time."""

    def __init__(self, stream: TextIO | None = None) -> None:
        super().__init__()
        self._stream = stream

    def emit(self, record: logging.LogRecord) -> None:
        try:
            stream = self._stream if self._stream is not None else sys.stderr
            if stream is None:
                return
            msg = self.format(record)
            stream.write(msg + "\n")
            try:
                stream.flush()
            except Exception:
                pass
        except Exception:
            self.handleError(record)


def resolve_log_level(raw: str | None = None) -> int:
    """Parse ``HERMES_WEBUI_LOG_LEVEL`` (default INFO)."""
    value = (raw if raw is not None else os.getenv("HERMES_WEBUI_LOG_LEVEL", "INFO")).strip().upper()
    if value == "WARN":
        value = "WARNING"
    return getattr(logging, value, logging.INFO)


def is_debug_level(level: int | None = None) -> bool:
    resolved = level if level is not None else resolve_log_level()
    return resolved <= logging.DEBUG


def _console_logger() -> logging.Logger:
    return logging.getLogger(f"{_ROOT_LOGGER_NAME}.console")


def configure_logging(
    *,
    level: int | None = None,
    force: bool = False,
    stream: TextIO | None = None,
) -> logging.Logger:
    """Configure project logging once. Idempotent unless ``force`` is True."""
    with _CONFIGURE_LOCK:
        project = logging.getLogger(_ROOT_LOGGER_NAME)
        if getattr(project, _CONFIGURED_ATTR, False) and not force and stream is None:
            return project

        resolved = level if level is not None else resolve_log_level()

        root = logging.getLogger()
        root.setLevel(resolved)
        if force:
            root.handlers = [h for h in root.handlers if not isinstance(h, _CurrentStderrHandler)]
        if not any(isinstance(h, _CurrentStderrHandler) for h in root.handlers):
            module_handler = _CurrentStderrHandler(stream)
            module_handler.setFormatter(
                logging.Formatter(
                    "%(asctime)s %(levelname)s %(name)s %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S",
                )
            )
            root.addHandler(module_handler)

        console = _console_logger()
        if force:
            console.handlers.clear()
        if not console.handlers:
            console_handler = _CurrentStderrHandler(stream)
            console_handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
            console.addHandler(console_handler)
        console.setLevel(resolved)
        console.propagate = False

        project.setLevel(resolved)
        project.propagate = False

        for name, noisy_level in _THIRD_PARTY_LOGGERS.items():
            logging.getLogger(name).setLevel(noisy_level)

        setattr(project, _CONFIGURED_ATTR, True)
        return project


def get_logger(name: str) -> logging.Logger:
    """Return a logger for application modules."""
    if name in ("console", f"{_ROOT_LOGGER_NAME}.console"):
        return _console_logger()
    if name == _ROOT_LOGGER_NAME or name.startswith(f"{_ROOT_LOGGER_NAME}."):
        return logging.getLogger(name)
    return logging.getLogger(name)


def log_line(level: int, line: str) -> None:
    """Emit a pre-formatted console line without raising."""
    try:
        configure_logging()
        _console_logger().log(level, "%s", line)
    except Exception:
        pass


def log_info(line: str) -> None:
    log_line(logging.INFO, line)


def log_warning(line: str) -> None:
    log_line(logging.WARNING, line)


def log_error(line: str) -> None:
    log_line(logging.ERROR, line)


def log_exception_line(line: str, *, exc_info: bool = True) -> None:
    try:
        configure_logging()
        _console_logger().log(logging.ERROR, "%s", line, exc_info=exc_info)
    except Exception:
        pass

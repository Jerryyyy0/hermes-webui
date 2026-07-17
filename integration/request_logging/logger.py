"""Safe standard-library console logging for WebUI pretty lines."""

from __future__ import annotations

import logging
import sys

_CONSOLE_LOGGER_NAME = "hermes.webui.console"
_CONFIGURED_ATTR = "_hermes_webui_console_configured"


class _CurrentStderrHandler(logging.Handler):
    """Logging handler that writes to the current stderr object at emit time."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            stream = sys.stderr
            if stream is None:
                return
            stream.write(self.format(record) + "\n")
            try:
                stream.flush()
            except Exception:
                pass
        except Exception:
            self.handleError(record)


def get_console_logger() -> logging.Logger:
    """Return the dedicated WebUI console logger, configured once."""
    logger = logging.getLogger(_CONSOLE_LOGGER_NAME)
    if not getattr(logger, _CONFIGURED_ATTR, False):
        handler = _CurrentStderrHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        setattr(logger, _CONFIGURED_ATTR, True)
    return logger


def safe_console_log(level: int, line: str) -> None:
    """Emit a console line without letting logging failures affect responses."""
    try:
        get_console_logger().log(level, "%s", line)
    except Exception:
        pass


def console_info(line: str) -> None:
    safe_console_log(logging.INFO, line)


def console_warning(line: str) -> None:
    safe_console_log(logging.WARNING, line)


def console_error(line: str) -> None:
    safe_console_log(logging.ERROR, line)

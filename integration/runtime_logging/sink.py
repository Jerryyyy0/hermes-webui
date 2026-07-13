"""Persistent stdout/stderr sink for direct ``server.py`` runs.

``bootstrap.py`` and process supervisors already own stdout/stderr persistence.
This module is for the direct-entry case, where ``python server.py`` would
otherwise leave request and crash diagnostics only in the terminal.
"""

from __future__ import annotations

import atexit
import io
import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

_FALSE_VALUES = {"0", "false", "no", "off"}
_TRUE_VALUES = {"1", "true", "yes", "on"}
_DEFAULT_MAX_BYTES = 10 * 1024 * 1024
_DEFAULT_BACKUP_COUNT = 5
_STDOUT_WRAPPED = False
_STDERR_WRAPPED = False
_SETUP: "RuntimeLogSetup | None" = None
_SETUP_LOCK = threading.RLock()


@dataclass(frozen=True)
class RuntimeLogSetup:
    """Result of runtime log initialization."""

    enabled: bool
    log_path: Path | None = None
    crash_log_path: Path | None = None
    crash_stream: TextIO | None = None
    reason: str | None = None


def _env_disabled(name: str, default_enabled: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return not default_enabled
    return str(raw).strip().lower() in _FALSE_VALUES


def _env_enabled(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in _TRUE_VALUES


def _env_positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _safe_write(stream: TextIO | None, text: str) -> None:
    if stream is None:
        return
    try:
        stream.write(text)
        try:
            stream.flush()
        except Exception:
            pass
    except Exception:
        pass


class SizeRotatingLogFile:
    """Small stdlib-only size rotating file used behind stdout/stderr tee."""

    def __init__(self, path: Path, *, max_bytes: int, backup_count: int):
        self.path = path
        self.max_bytes = max_bytes
        self.backup_count = backup_count
        self._lock = threading.RLock()
        self._file: TextIO | None = None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._open()

    @property
    def encoding(self) -> str:
        return "utf-8"

    def _open(self) -> None:
        self._file = self.path.open("a", encoding="utf-8", buffering=1)

    def _should_rotate(self, incoming: str) -> bool:
        if self.max_bytes <= 0:
            return False
        try:
            current = self.path.stat().st_size if self.path.exists() else 0
        except OSError:
            current = 0
        return current > 0 and current + len(incoming.encode("utf-8", "replace")) > self.max_bytes

    def _rotate_locked(self) -> None:
        if self._file is not None:
            try:
                self._file.flush()
            except Exception:
                pass
            try:
                self._file.close()
            except Exception:
                pass
            self._file = None
        if self.backup_count > 0:
            oldest = self.path.with_name(f"{self.path.name}.{self.backup_count}")
            try:
                oldest.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
            for index in range(self.backup_count - 1, 0, -1):
                src = self.path.with_name(f"{self.path.name}.{index}")
                dst = self.path.with_name(f"{self.path.name}.{index + 1}")
                if src.exists():
                    try:
                        src.replace(dst)
                    except OSError:
                        pass
            if self.path.exists():
                try:
                    self.path.replace(self.path.with_name(f"{self.path.name}.1"))
                except OSError:
                    pass
        else:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
        self._open()

    def write(self, text: str) -> int:
        if not isinstance(text, str):
            text = str(text)
        with self._lock:
            if self._file is None:
                self._open()
            if self._should_rotate(text):
                self._rotate_locked()
            assert self._file is not None
            self._file.write(text)
            self._file.flush()
        return len(text)

    def flush(self) -> None:
        with self._lock:
            if self._file is not None:
                self._file.flush()

    def close(self) -> None:
        with self._lock:
            if self._file is not None:
                try:
                    self._file.flush()
                finally:
                    self._file.close()
                    self._file = None


class TeeTextIO(io.TextIOBase):
    """Text stream that mirrors writes to the original stream and a log file."""

    def __init__(self, primary: TextIO | None, sink: SizeRotatingLogFile):
        self._primary = primary
        self._sink = sink
        self._lock = threading.RLock()

    @property
    def encoding(self) -> str:
        return getattr(self._primary, "encoding", None) or "utf-8"

    @property
    def errors(self) -> str:
        return getattr(self._primary, "errors", None) or "replace"

    def writable(self) -> bool:
        return True

    def isatty(self) -> bool:
        try:
            return bool(self._primary and self._primary.isatty())
        except Exception:
            return False

    def fileno(self) -> int:
        if self._primary is None:
            raise OSError("no primary stream")
        return self._primary.fileno()

    def write(self, text: str) -> int:
        if not isinstance(text, str):
            text = str(text)
        with self._lock:
            _safe_write(self._primary, text)
            try:
                self._sink.write(text)
            except Exception:
                pass
        return len(text)

    def flush(self) -> None:
        with self._lock:
            try:
                if self._primary is not None:
                    self._primary.flush()
            except Exception:
                pass
            try:
                self._sink.flush()
            except Exception:
                pass


def _default_log_path(state_dir: Path, port: int) -> Path:
    return state_dir / f"server-{port}.log"


def _crash_log_path(log_path: Path, port: int) -> Path:
    if os.getenv("HERMES_WEBUI_SERVER_CRASH_LOG_PATH", "").strip():
        return Path(os.environ["HERMES_WEBUI_SERVER_CRASH_LOG_PATH"]).expanduser().resolve()
    default_name = f"server-{port}-crash.log"
    if log_path.name.startswith("server-") and log_path.name.endswith(".log"):
        default_name = f"{log_path.stem}-crash.log"
    return log_path.with_name(default_name)


def setup_runtime_logging(*, state_dir: Path, port: int) -> RuntimeLogSetup:
    """Install persistent direct-entry logging and return crash stream info.

    The function is intentionally best-effort. If anything about the log path is
    invalid or unwritable, it reports a warning to the original stderr and lets
    the WebUI continue without persistent direct-entry logs.
    """

    global _SETUP, _STDOUT_WRAPPED, _STDERR_WRAPPED
    with _SETUP_LOCK:
        if _SETUP is not None:
            return _SETUP

        if _env_disabled("HERMES_WEBUI_SERVER_LOG", default_enabled=True):
            _SETUP = RuntimeLogSetup(enabled=False, reason="disabled")
            return _SETUP
        if _env_enabled("HERMES_WEBUI_SERVER_LOG_EXTERNAL"):
            _SETUP = RuntimeLogSetup(enabled=False, reason="external")
            return _SETUP

        state_path = Path(state_dir).expanduser().resolve()
        raw_log_path = os.getenv("HERMES_WEBUI_SERVER_LOG_PATH", "").strip()
        log_path = Path(raw_log_path).expanduser().resolve() if raw_log_path else _default_log_path(state_path, port)
        max_bytes = _env_positive_int("HERMES_WEBUI_SERVER_LOG_MAX_BYTES", _DEFAULT_MAX_BYTES)
        backup_count = _env_positive_int("HERMES_WEBUI_SERVER_LOG_BACKUP_COUNT", _DEFAULT_BACKUP_COUNT)
        crash_path = _crash_log_path(log_path, port)

        original_stdout = sys.stdout
        original_stderr = sys.stderr
        try:
            sink = SizeRotatingLogFile(log_path, max_bytes=max_bytes, backup_count=backup_count)
            crash_path.parent.mkdir(parents=True, exist_ok=True)
            crash_stream = crash_path.open("a", encoding="utf-8", buffering=1)
        except Exception as exc:
            _safe_write(original_stderr, f"[webui] WARNING: runtime log persistence disabled: {exc}\n")
            _SETUP = RuntimeLogSetup(enabled=False, reason="open_failed")
            return _SETUP

        if not isinstance(sys.stdout, TeeTextIO):
            sys.stdout = TeeTextIO(original_stdout, sink)  # type: ignore[assignment]
            _STDOUT_WRAPPED = True
        if not isinstance(sys.stderr, TeeTextIO):
            sys.stderr = TeeTextIO(original_stderr, sink)  # type: ignore[assignment]
            _STDERR_WRAPPED = True

        def _close_logs() -> None:
            try:
                crash_stream.flush()
                crash_stream.close()
            except Exception:
                pass
            try:
                sink.close()
            except Exception:
                pass

        try:
            atexit.register(_close_logs)
        except Exception:
            pass

        _SETUP = RuntimeLogSetup(
            enabled=True,
            log_path=log_path,
            crash_log_path=crash_path,
            crash_stream=crash_stream,
        )
        print(f"[webui] Runtime log file: {log_path}", flush=True)
        print(f"[webui] Crash diagnostic log file: {crash_path}", flush=True)
        return _SETUP

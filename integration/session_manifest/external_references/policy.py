"""Policy and race-safe file opening for external artifact references."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from api.config import STATE_DIR

_IGNORED_PARTS = frozenset({
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__",
    "dist", "build", ".next", ".cache", "uploads",
})
_SENSITIVE_BASENAMES = frozenset({
    ".env", "auth.json", "auth.lock", "config.json", "config.yaml", "config.yml", "state.db",
    "state.db-wal", "state.db-shm", ".signing_key", ".pbkdf2_key", ".sessions.json",
    "google_token.json", "google_client_secret.json", "gateway_state.json",
    "channel_directory.json", "jobs.json", "passkeys.json", ".passkey_challenges.json",
    ".login_attempts.json",
})
# Chat attachments and agent memories are deliberately not part of the
# state-root denylist.  They remain inaccessible by path alone: the preview
# route requires an exact, persisted Artifact record before this policy is
# consulted.  This allows a verified file to use the same read-only Artifact
# preview as another external file without exposing either directory for
# enumeration.
_STATE_SUBDIRS = frozenset({"sessions", "cron", "logs", "checkpoints", "backups"})
_SYSTEM_DENY_ROOTS = ("/etc", "/private/etc", "/var/db", "/private/var/db", "/System")
_DIR_FD_OK = os.open in getattr(os, "supports_dir_fd", set())
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)


def normalize_external_path(raw_path: str | Path | None) -> Path | None:
    """Return a lexical absolute path without resolving symlinks."""
    try:
        path = Path(str(raw_path or "")).expanduser()
    except (TypeError, ValueError, OSError):
        return None
    if not path.is_absolute():
        return None
    try:
        return Path(os.path.normpath(os.path.abspath(str(path))))
    except (TypeError, ValueError, OSError):
        return None


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _hermes_roots() -> list[Path]:
    roots: list[Path] = []
    raw_homes = [
        os.getenv("HERMES_HOME", ""),
        str(Path.home() / ".hermes"),
        str(STATE_DIR),
    ]
    try:
        from api.profiles import _DEFAULT_HERMES_HOME

        raw_homes.append(str(_DEFAULT_HERMES_HOME))
    except (ImportError, AttributeError):
        pass
    for raw in raw_homes:
        try:
            root = normalize_external_path(raw)
        except (TypeError, ValueError, OSError):
            root = None
        if root is not None and root not in roots:
            roots.append(root)
    return roots


def _protected_roots() -> list[Path]:
    roots: list[Path] = []
    for raw in _SYSTEM_DENY_ROOTS:
        path = normalize_external_path(raw)
        if path is not None:
            roots.append(path)
    for raw in (Path.home() / ".ssh", Path.home() / ".gnupg"):
        path = normalize_external_path(raw)
        if path is not None:
            roots.append(path)
    for home in _hermes_roots():
        for subdir in _STATE_SUBDIRS:
            roots.append(home / subdir)
        for subdir in _STATE_SUBDIRS:
            roots.append(home / "webui_state" / subdir)
    return roots


def external_path_is_allowed(raw_path: str | Path | None) -> Path | None:
    """Return a policy-allowed external path, before opening it."""
    path = normalize_external_path(raw_path)
    if path is None or path.name.casefold() in _SENSITIVE_BASENAMES:
        return None
    if any(part.casefold() in _IGNORED_PARTS for part in path.parts):
        return None
    if any(_is_within(path, root) for root in _protected_roots()):
        return None
    return path


def open_external_regular_file(raw_path: str | Path | None) -> tuple[int, Path, os.stat_result] | None:
    """Open a policy-allowed regular file without following any path component."""
    path = external_path_is_allowed(raw_path)
    if path is None or not _DIR_FD_OK or not _O_NOFOLLOW:
        return None
    parts = path.parts
    if not parts or parts[0] != os.sep or len(parts) < 2:
        return None
    try:
        fd = os.open(os.sep, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW)
    except OSError:
        return None
    try:
        for index, part in enumerate(parts[1:]):
            is_last = index == len(parts) - 2
            flags = os.O_RDONLY | _O_NOFOLLOW
            if not is_last:
                flags |= _O_DIRECTORY
            next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        file_stat = os.fstat(fd)
        if not stat.S_ISREG(file_stat.st_mode):
            os.close(fd)
            return None
        return fd, path, file_stat
    except OSError:
        try:
            os.close(fd)
        except OSError:
            pass
        return None


def close_fd_quietly(fd: int | None) -> None:
    if fd is None:
        return
    try:
        os.close(fd)
    except OSError:
        pass

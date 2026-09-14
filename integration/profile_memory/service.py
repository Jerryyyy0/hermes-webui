"""Read and edit profile memory while maintaining an append-only timeline."""

from __future__ import annotations

import difflib
import json
import os
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import yaml

from api.paths import _atomic_write_text
from integration.profile_overview.service import resolve_profile

SHANGHAI = ZoneInfo("Asia/Shanghai")
ENTRY_DELIMITER = "\n§\n"
EVENTS_FILENAME = "memory_events.jsonl"
DESCRIPTION = "跨项目生效的助理记忆，包括用户偏好、工作背景、历史偏好和常用知识"
TARGET_FILES = {"memory": "MEMORY.md"}
TARGET_LIMITS = {"memory": 2200}
ACTION_NAMES = {"created": "形成记忆", "updated": "修改记忆", "deleted": "删除记忆"}

msvcrt = None
try:
    import fcntl
except ImportError:
    fcntl = None
    try:
        import msvcrt
    except ImportError:
        pass


def _iso_seconds(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=SHANGHAI)
    return value.astimezone(SHANGHAI).isoformat(timespec="seconds")


def _from_timestamp(value: float) -> str:
    return _iso_seconds(datetime.fromtimestamp(value, tz=SHANGHAI))


def _memory_dir(home: Path) -> Path:
    return Path(home) / "memories"


def _target_path(home: Path, target: str) -> Path:
    normalized = str(target or "").strip().lower()
    filename = TARGET_FILES.get(normalized)
    if filename is None:
        raise ValueError("target 只能是 memory")
    return _memory_dir(home) / filename


def _target_limit(home: Path, target: str) -> int:
    default = TARGET_LIMITS[target]
    config_path = Path(home) / "config.yaml"
    if not config_path.is_file() or config_path.is_symlink():
        return default
    try:
        config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        memory = config.get("memory") if isinstance(config, dict) else None
        key = "memory_char_limit"
        value = int(memory.get(key, default)) if isinstance(memory, dict) else default
        return value if value > 0 else default
    except (OSError, UnicodeError, TypeError, ValueError, yaml.YAMLError):
        return default


def _read_text(path: Path) -> str:
    if not path.exists():
        return ""
    if path.is_symlink() or not path.is_file():
        raise OSError(f"记忆文件不是普通文件: {path.name}")
    return path.read_text(encoding="utf-8")


def _parse_entries(content: str) -> list[str]:
    normalized = str(content).replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []
    return [item.strip() for item in normalized.split(ENTRY_DELIMITER) if item.strip()]


def _render_entries(entries: Iterable[str]) -> str:
    return ENTRY_DELIMITER.join(str(item).strip() for item in entries if str(item).strip())


@contextmanager
def _file_lock(path: Path):
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = open(lock_path, "a+", encoding="utf-8")
    try:
        if fcntl:
            fcntl.flock(fd, fcntl.LOCK_EX)
        elif msvcrt:
            fd.seek(0)
            msvcrt.locking(fd.fileno(), msvcrt.LK_LOCK, 1)
        yield
    finally:
        if fcntl:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except (OSError, IOError):
                pass
        elif msvcrt:
            try:
                fd.seek(0)
                msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)
            except (OSError, IOError):
                pass
        fd.close()


def _diff_events(old_entries: list[str], new_entries: list[str], target: str) -> list[dict[str, str]]:
    events: list[dict[str, str]] = []
    matcher = difflib.SequenceMatcher(a=old_entries, b=new_entries, autojunk=False)
    for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
        if tag == "equal":
            continue
        old_slice = old_entries[old_start:old_end]
        new_slice = new_entries[new_start:new_end]
        if tag == "replace":
            paired = min(len(old_slice), len(new_slice))
            events.extend(
                {"target": target, "action": "updated", "content": new_content}
                for old_content, new_content in zip(old_slice[:paired], new_slice[:paired])
                if old_content != new_content
            )
            events.extend(
                {"target": target, "action": "deleted", "content": content}
                for content in old_slice[paired:]
            )
            events.extend(
                {"target": target, "action": "created", "content": content}
                for content in new_slice[paired:]
            )
        elif tag == "delete":
            events.extend(
                {"target": target, "action": "deleted", "content": content}
                for content in old_slice
            )
        elif tag == "insert":
            events.extend(
                {"target": target, "action": "created", "content": content}
                for content in new_slice
            )
    return events


def _append_memory_events(
    home: Path,
    events: Iterable[dict[str, str]],
    *,
    source: str,
    now: datetime,
) -> list[dict[str, Any]]:
    timestamp = _iso_seconds(now)
    rows = [
        {
            "event_id": f"mem_evt_{uuid.uuid4().hex}",
            "target": event["target"],
            "action": event["action"],
            "occurred_at": timestamp,
            "content": event["content"],
            "source": source,
        }
        for event in events
    ]
    if not rows:
        return []
    path = _memory_dir(home) / EVENTS_FILENAME
    if path.is_symlink():
        raise OSError("记忆时间线文件不能是符号链接")
    payload = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows).encode("utf-8")
    with _file_lock(path):
        with open(path, "a+b") as stream:
            stream.seek(0, os.SEEK_END)
            original_size = stream.tell()
            try:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            except Exception:
                stream.seek(original_size)
                stream.truncate()
                stream.flush()
                raise
    return rows


def read_memory_events(home: Path) -> list[dict[str, Any]]:
    path = _memory_dir(home) / EVENTS_FILENAME
    if not path.exists():
        return []
    if path.is_symlink() or not path.is_file():
        raise OSError("记忆时间线文件不是普通文件")
    rows: list[dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as stream:
        for line in stream:
            try:
                row = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(row, dict):
                continue
            action = str(row.get("action") or "")
            target = str(row.get("target") or "")
            content = str(row.get("content") or "").strip()
            source = str(row.get("source") or "")
            event_id = str(row.get("event_id") or "")
            occurred_at = str(row.get("occurred_at") or "")
            if (
                action not in ACTION_NAMES
                or target not in TARGET_FILES
                or source not in {"agent", "webui"}
                or not event_id
                or not occurred_at
                or not content
            ):
                continue
            rows.append(row)
    return rows


def build_memory_overview(profile_name: str) -> dict[str, Any]:
    profile, home = resolve_profile(profile_name)
    info = profile.get("info") if isinstance(profile.get("info"), dict) else {}
    memories: dict[str, dict[str, Any]] = {}
    modified: list[float] = []
    for target, filename in TARGET_FILES.items():
        path = _memory_dir(home) / filename
        content = _read_text(path)
        mtime = path.stat().st_mtime if path.is_file() and not path.is_symlink() else None
        if mtime is not None:
            modified.append(mtime)
        memories[target] = {
            "filename": filename,
            "content": content,
            "updated_at": _from_timestamp(mtime) if mtime is not None else None,
        }
    events = read_memory_events(home)
    latest_event = str(events[-1].get("occurred_at") or "") if events else ""
    file_updated_at = _from_timestamp(max(modified)) if modified else None
    updated_at = max(
        (value for value in (file_updated_at, latest_event or None) if value is not None),
        default=None,
    )
    return {
        "profile": str(profile.get("name") or profile_name),
        "assistant_name": str(info.get("display_name") or profile.get("name") or profile_name),
        "description": DESCRIPTION,
        "updated_at": updated_at,
        "memories": memories,
    }


def update_memory(
    profile_name: str,
    target: str,
    content: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not isinstance(content, str):
        raise ValueError("content 必须是字符串")
    profile, home = resolve_profile(profile_name)
    normalized_target = str(target or "").strip().lower()
    path = _target_path(home, normalized_target)
    memory_dir = _memory_dir(home)
    memory_dir.mkdir(parents=True, exist_ok=True)
    new_entries = _parse_entries(content)
    normalized_content = _render_entries(new_entries)
    limit = _target_limit(home, normalized_target)
    if len(normalized_content) > limit:
        raise ValueError(f"{TARGET_FILES[normalized_target]} 内容不能超过 {limit} 个字符")

    with _file_lock(path):
        if path.is_symlink():
            raise OSError("记忆文件不能是符号链接")
        existed = path.exists()
        old_content = _read_text(path)
        old_entries = _parse_entries(old_content)
        events = _diff_events(old_entries, new_entries, normalized_target)
        _atomic_write_text(path, normalized_content, encoding="utf-8")
        try:
            _append_memory_events(
                home,
                events,
                source="webui",
                now=now or datetime.now(SHANGHAI),
            )
        except Exception:
            if existed:
                _atomic_write_text(path, old_content, encoding="utf-8")
            else:
                path.unlink(missing_ok=True)
            raise

    updated_at = _from_timestamp(path.stat().st_mtime)
    return {
        "ok": True,
        "profile": str(profile.get("name") or profile_name),
        "target": normalized_target,
        "filename": TARGET_FILES[normalized_target],
        "content": normalized_content,
        "updated_at": updated_at,
        "events_created": len(events),
    }


def build_memory_timeline(profile_name: str, *, page: int = 1, page_size: int = 20) -> dict[str, Any]:
    if page < 1:
        raise ValueError("page 必须大于等于 1")
    if page_size < 1 or page_size > 100:
        raise ValueError("page_size 必须在 1 到 100 之间")
    profile, home = resolve_profile(profile_name)
    events = list(reversed(read_memory_events(home)))
    total = len(events)
    offset = (page - 1) * page_size
    selected = events[offset : offset + page_size]
    items = [
        {
            "event_id": str(event.get("event_id") or ""),
            "target": event["target"],
            "event_type": event["action"],
            "event_name": ACTION_NAMES[event["action"]],
            "occurred_at": str(event.get("occurred_at") or ""),
            "content": str(event.get("content") or ""),
            "source": str(event.get("source") or ""),
        }
        for event in selected
    ]
    return {
        "profile": str(profile.get("name") or profile_name),
        "items": items,
        "pagination": {
            "page": page,
            "page_size": page_size,
            "total": total,
            "has_more": offset + len(items) < total,
        },
    }

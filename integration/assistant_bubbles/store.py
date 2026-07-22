"""Independent assistant_bubbles.json persistence."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
STORE_FILENAME = "assistant_bubbles.json"
ITEM_ORDER = [
    "assistant_intro",
    "emotion",
    "scheduled_task",
    "emotion",
    "memory",
    "emotion",
    "skill",
    "emotion",
]
MODEL_CATEGORIES = ("assistant_intro", "memory", "skill", "emotion")


def store_path(profile_path: Path) -> Path:
    return Path(profile_path) / STORE_FILENAME


def empty_store() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "items": [
            {"type": item_type, "text": None, **({"dynamic": True} if item_type == "scheduled_task" else {})}
            for item_type in ITEM_ORDER
        ],
        "generation": {},
    }


def _valid_text(value: Any) -> bool:
    return value is None or (isinstance(value, str) and len(value) > 0)


def _validate_generation(generation: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(generation, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for category in MODEL_CATEGORIES:
        raw = generation.get(category)
        if not isinstance(raw, dict):
            continue
        entry = {
            "fingerprint": str(raw.get("fingerprint") or ""),
            "generated_at": raw.get("generated_at"),
            "last_attempt_at": raw.get("last_attempt_at"),
            "retry_after": raw.get("retry_after"),
        }
        if isinstance(entry["generated_at"], (int, float)) or entry["generated_at"] is None:
            if isinstance(entry["last_attempt_at"], (int, float)) or entry["last_attempt_at"] is None:
                if isinstance(entry["retry_after"], (int, float)) or entry["retry_after"] is None:
                    out[category] = entry
    return out


def validate_store(data: Any) -> dict[str, Any] | None:
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        return None
    items = data.get("items")
    if not isinstance(items, list) or len(items) != len(ITEM_ORDER):
        return None
    normalized_items: list[dict[str, Any]] = []
    emotion_count = 0
    for idx, expected_type in enumerate(ITEM_ORDER):
        raw = items[idx]
        if not isinstance(raw, dict) or raw.get("type") != expected_type:
            return None
        text = raw.get("text")
        if not _valid_text(text):
            return None
        item = {"type": expected_type, "text": text}
        if expected_type == "scheduled_task":
            item["text"] = None
            item["dynamic"] = True
        if expected_type == "emotion":
            emotion_count += 1
        normalized_items.append(item)
    if emotion_count != 4:
        return None
    return {
        "schema_version": SCHEMA_VERSION,
        "items": normalized_items,
        "generation": _validate_generation(data.get("generation")),
    }


def read_store(profile_path: Path) -> dict[str, Any] | None:
    path = store_path(profile_path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return validate_store(data)


def write_store(profile_path: Path, data: dict[str, Any]) -> None:
    normalized = validate_store(data)
    if normalized is None:
        raise ValueError("assistant_bubbles store schema is invalid")
    profile_path = Path(profile_path)
    profile_path.mkdir(parents=True, exist_ok=True)
    path = store_path(profile_path)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{STORE_FILENAME}.", suffix=".tmp", dir=str(profile_path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(normalized, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def update_category(
    profile_path: Path,
    category: str,
    texts: str | list[str] | None,
    generation_entry: dict[str, Any],
) -> dict[str, Any]:
    data = read_store(profile_path) or empty_store()
    items = data["items"]
    if category == "emotion":
        if not isinstance(texts, list) or len(texts) != 4:
            raise ValueError("emotion requires four texts")
        text_iter = iter(texts)
        for item in items:
            if item.get("type") == "emotion":
                item["text"] = next(text_iter)
    elif category in ("assistant_intro", "memory", "skill"):
        if not isinstance(texts, str):
            raise ValueError(f"{category} requires one text")
        for item in items:
            if item.get("type") == category:
                item["text"] = texts
                break
    else:
        raise ValueError(f"unsupported category: {category}")
    data.setdefault("generation", {})[category] = generation_entry
    write_store(profile_path, data)
    return data


def update_generation_only(profile_path: Path, category: str, generation_entry: dict[str, Any]) -> dict[str, Any]:
    data = read_store(profile_path) or empty_store()
    data.setdefault("generation", {})[category] = generation_entry
    write_store(profile_path, data)
    return data

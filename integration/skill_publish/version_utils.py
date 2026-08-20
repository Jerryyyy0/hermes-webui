"""Semver helpers for skill publish versions (docs §6)."""

from __future__ import annotations

import time
from datetime import datetime


def compute_next_version(current: str | None) -> str:
    """First publish -> 1.0.0; otherwise patch+1 (major/minor unchanged)."""
    raw = str(current or "").strip()
    if not raw:
        return "1.0.0"
    parts = raw.split(".")
    if len(parts) != 3:
        return "1.0.0"
    try:
        major, minor, patch = (int(p) for p in parts)
    except ValueError:
        return "1.0.0"
    return f"{major}.{minor}.{patch + 1}"


def semver_gt(a: str, b: str) -> bool:
    def parse(v: str) -> tuple[int, int, int]:
        parts = str(v or "").split(".")
        nums: list[int] = []
        for p in parts[:3]:
            try:
                nums.append(int(p))
            except ValueError:
                nums.append(0)
        while len(nums) < 3:
            nums.append(0)
        return (nums[0], nums[1], nums[2])

    return parse(a) > parse(b)


def parse_apply_time(value: str | None) -> float | None:
    """Parse upstream ``applyTime`` (``yyyy-MM-dd HH:mm:ss``, local time) -> epoch."""
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").timestamp()
    except ValueError:
        return None


def now() -> float:
    return time.time()

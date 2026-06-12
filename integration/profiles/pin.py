"""Profile pin state stored in info.json (pinned, pin_order)."""

from __future__ import annotations

from pathlib import Path

from integration.profiles.memory_snapshot import load_memory_snapshot
from integration.profiles.write import (
    _enriched_profile_entry,
    _read_info_file,
    _resolve_profile_path,
    _write_info_file,
)

PROFILE_PIN_LIMIT = 5


def list_all_pin_orders() -> dict[str, int]:
    from api.profiles import list_profiles_api

    result: dict[str, int] = {}
    for entry in list_profiles_api():
        name = entry.get("name")
        path = entry.get("path")
        if not name or not path:
            continue
        info = _read_info_file(Path(str(path)).expanduser())
        if info.get("pinned") is True:
            order = info.get("pin_order")
            result[str(name)] = order if isinstance(order, int) else 0
    return result


def set_profile_pinned(name: str, pinned: bool) -> dict:
    name = str(name or "").strip()
    if not name:
        raise ValueError("name is required")
    profile_path = _resolve_profile_path(name)
    info = _read_info_file(profile_path)

    if pinned:
        already_pinned = info.get("pinned") is True
        if not already_pinned:
            pinned_orders = list_all_pin_orders()
            if len(pinned_orders) >= PROFILE_PIN_LIMIT:
                raise ValueError(
                    f"Up to {PROFILE_PIN_LIMIT} profiles can be pinned. "
                    "Unpin one before pinning another."
                )
            for pinned_name, order in pinned_orders.items():
                pinned_path = _resolve_profile_path(pinned_name)
                pinned_info = _read_info_file(pinned_path)
                pinned_info["pin_order"] = order + 1
                _write_info_file(pinned_path, pinned_info)
            info["pinned"] = True
            info["pin_order"] = 1
    else:
        info.pop("pinned", None)
        info.pop("pin_order", None)

    if info:
        _write_info_file(profile_path, info)
    else:
        info_path = profile_path / "info.json"
        if info_path.is_file():
            info_path.unlink()

    profile = _enriched_profile_entry(name, profile_path)
    profile["memory_snapshot"] = load_memory_snapshot(str(profile_path))
    return {"ok": True, "profile": profile}

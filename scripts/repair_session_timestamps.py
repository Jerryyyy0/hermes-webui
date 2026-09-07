#!/usr/bin/env python3
"""Safely reconcile one WebUI sidecar's message timestamps with state.db."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.models import (  # noqa: E402
    _active_stream_ids,
    _get_profile_home,
    get_state_db_session_messages,
    reconcile_message_timestamps,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--apply", action="store_true", help="写入修复；默认 dry-run")
    parser.add_argument("--dry-run", action="store_true", help="仅报告，不写入（默认行为）")
    parser.add_argument("--backup-dir", type=Path)
    args = parser.parse_args()
    if args.apply and args.dry_run:
        parser.error("--apply 与 --dry-run 不能同时使用")

    sid = str(args.session_id).strip()
    profile = str(args.profile).strip()
    if not sid or not profile or not all(c.isalnum() or c in "_-" for c in sid):
        print(json.dumps({"status": "invalid_session_or_profile", "dry_run": not args.apply}, ensure_ascii=False))
        return 1
    try:
        home = _get_profile_home(profile)
    except Exception as exc:
        print(json.dumps({"status": "invalid_profile", "error": str(exc), "dry_run": not args.apply}, ensure_ascii=False, indent=2))
        return 1
    # WebUI sidecars live in the WebUI state directory beneath the profile
    # home; the agent database remains at the profile root.
    sidecar_path = home / "webui" / "sessions" / f"{sid}.json"
    db_path = home / "state.db"
    if not sidecar_path.exists() or not db_path.exists():
        print(json.dumps({"status": "missing_sidecar_or_state_db", "session_id": sid, "profile": profile, "dry_run": not args.apply}, ensure_ascii=False, indent=2))
        return 1
    try:
        raw = json.loads(sidecar_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(json.dumps({"status": "invalid_sidecar", "error": str(exc), "dry_run": not args.apply}, ensure_ascii=False, indent=2))
        return 1
    messages = raw.get("messages")
    if not isinstance(messages, list):
        print(json.dumps({"status": "invalid_messages", "dry_run": not args.apply}, ensure_ascii=False, indent=2))
        return 1
    if raw.get("active_stream_id") or raw.get("pending_user_message") or raw.get("pending_next_turns"):
        print(json.dumps({"status": "active_stream_or_pending_turn", "dry_run": not args.apply}, ensure_ascii=False, indent=2))
        return 1
    try:
        if str(raw.get("active_stream_id") or "") in _active_stream_ids():
            print(json.dumps({"status": "active_stream_or_pending_turn", "dry_run": not args.apply}, ensure_ascii=False, indent=2))
            return 1
    except Exception:
        pass

    state = get_state_db_session_messages(sid, profile=profile, include_ids=True)
    report = reconcile_message_timestamps(messages, state)
    changes = [
        {"index": i, "from": messages[i].get("timestamp"), "to": report["messages"][i].get("timestamp")}
        for i in range(min(len(messages), len(report["messages"])))
        if isinstance(messages[i], dict) and messages[i].get("timestamp") != report["messages"][i].get("timestamp")
    ]
    result = {k: v for k, v in report.items() if k != "messages"}
    result.update({"session_id": sid, "profile": profile, "changes": changes, "dry_run": not args.apply})
    if not changes:
        result["status"] = "no_change"
    elif args.apply:
        backup_dir = args.backup_dir
        if backup_dir is None:
            print(json.dumps({**result, "status": "backup_dir_required"}, ensure_ascii=False, indent=2))
            return 1
        backup_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(sidecar_path, backup_dir / sidecar_path.name)
        raw["messages"] = report["messages"]
        fd, tmp_name = tempfile.mkstemp(prefix=f".{sid}.", suffix=".tmp", dir=str(sidecar_path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(raw, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, sidecar_path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        result["status"] = "applied"
    else:
        result["status"] = "dry_run"
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

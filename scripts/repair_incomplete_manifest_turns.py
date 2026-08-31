#!/usr/bin/env python3
"""Dry-run or safely append strong-evidence artifacts to one manifest turn."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.models import Session  # noqa: E402
from integration.session_manifest.repair import repair_incomplete_manifest_turn  # noqa: E402
from integration.session_manifest.store import DB_FILENAME, STATE_DIR  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--session-id', required=True, help='完整 Session sidecar 的安全 session id')
    parser.add_argument('--turn-key', required=True, help='要补全的稳定真实 user turn key，例如 turn:1')
    parser.add_argument('--db', type=Path, help='可选的 session_manifest.db 路径')
    parser.add_argument('--apply', action='store_true', help='写入经预演核对的缺失记录；默认只预演')
    args = parser.parse_args()

    session = Session.load(args.session_id)
    if session is None:
        print(json.dumps({
            'status': 'session_not_found',
            'session_id': args.session_id,
            'turn_key': args.turn_key,
            'dry_run': not args.apply,
        }, ensure_ascii=False, indent=2))
        return 1

    report = repair_incomplete_manifest_turn(
        session,
        args.turn_key,
        apply=args.apply,
        db_path=args.db or STATE_DIR / DB_FILENAME,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get('status') in {'dry_run', 'no_change', 'applied'} else 1


if __name__ == '__main__':
    raise SystemExit(main())

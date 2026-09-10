#!/usr/bin/env python3
"""Dry-run or atomically rebuild contaminated Artifact turns for one session."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.models import Session  # noqa: E402
from integration.session_manifest.repair import repair_contaminated_manifest_session  # noqa: E402
from integration.session_manifest.store import DB_FILENAME, STATE_DIR  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--session-id', required=True, help='完整 Session sidecar 的安全 session id')
    parser.add_argument('--db', type=Path, help='可选的 session_manifest.db 路径')
    parser.add_argument('--backup', type=Path, help='应用前 SQLite 一致性备份路径')
    parser.add_argument('--apply', action='store_true', help='按预演结果原子替换记录；默认只预演')
    args = parser.parse_args()

    session = Session.load(args.session_id)
    if session is None:
        print(json.dumps({
            'status': 'session_not_found',
            'session_id': args.session_id,
            'dry_run': not args.apply,
        }, ensure_ascii=False, indent=2))
        return 1

    report = repair_contaminated_manifest_session(
        session,
        apply=args.apply,
        db_path=args.db or STATE_DIR / DB_FILENAME,
        backup_path=args.backup,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get('status') in {'dry_run', 'no_change', 'applied'} else 1


if __name__ == '__main__':
    raise SystemExit(main())

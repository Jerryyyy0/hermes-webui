#!/usr/bin/env python3
"""Inspect or explicitly rebind one orphan manifest turn."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.session_manifest_repair import rebind_manifest_turn_records  # noqa: E402


def rebind_manifest_turn(
    db_path: Path,
    *,
    lineage_key: str,
    profile: str,
    old_key: str,
    new_key: str,
    apply: bool = False,
) -> dict:
    """Return a dry-run report or atomically move an explicit turn mapping."""
    return rebind_manifest_turn_records(
        lineage_key=lineage_key,
        profile=profile,
        old_key=old_key,
        new_key=new_key,
        apply=apply,
        db_path=db_path,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True, type=Path, help='Path to session_manifest.db')
    parser.add_argument('--lineage-key', required=True)
    parser.add_argument('--profile', default='')
    parser.add_argument('--old-key', required=True)
    parser.add_argument('--new-key', required=True)
    parser.add_argument('--apply', action='store_true', help='Apply the explicit mapping')
    args = parser.parse_args()
    report = rebind_manifest_turn(
        args.db,
        lineage_key=args.lineage_key,
        profile=args.profile,
        old_key=args.old_key,
        new_key=args.new_key,
        apply=args.apply,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['status'] != 'not_found' else 1


if __name__ == '__main__':
    raise SystemExit(main())

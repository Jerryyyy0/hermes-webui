#!/usr/bin/env python3
"""Report how many on-disk workspace files lack a profile in session_manifest.db.

Walks the integration workspace root, collects every previewable file, then
cross-references against the artifact profile index in session_manifest.db.
Files not found in the index are "missing profile" (B-class).

Usage:
    python3 scripts/audit_workspace_profiles.py
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

# Make repo importable when run from anywhere.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.workspace import is_workspace_cruft_basename  # noqa: E402
from integration.workspace._root import integration_workspace_root  # noqa: E402
from api.session_manifest_store import (  # noqa: E402
    DB_FILENAME,
    get_artifact_profile_index,
)
from api.config import STATE_DIR  # noqa: E402


def collect_disk_files(root: Path) -> list[str]:
    """Return workspace-relative posix paths of previewable files under root."""
    files: list[str] = []
    for p in root.rglob("*"):
        if not p.is_file() or p.is_symlink():
            continue
        if is_workspace_cruft_basename(p.name):
            continue
        try:
            rel = p.resolve().relative_to(root.resolve())
        except ValueError:
            continue
        files.append(rel.as_posix())
    return sorted(files)


def main() -> int:
    root = integration_workspace_root()
    db_path = STATE_DIR / DB_FILENAME
    print(f"workspace root : {root}")
    print(f"manifest db    : {db_path}")
    print(f"db exists      : {db_path.exists()}")

    disk_files = collect_disk_files(root)
    index = get_artifact_profile_index()
    indexed_paths = set(index.keys())

    with_profile: list[str] = []
    without_profile: list[str] = []
    for rel in disk_files:
        if rel in indexed_paths:
            with_profile.append(rel)
        else:
            without_profile.append(rel)

    # Also report indexed paths that no longer exist on disk (stale rows).
    disk_set = set(disk_files)
    stale_indexed = sorted(indexed_paths - disk_set)

    print()
    print("== summary ==")
    print(f"disk files              : {len(disk_files)}")
    print(f"  with profile in DB    : {len(with_profile)}")
    print(f"  without profile (B)   : {len(without_profile)}")
    print(f"indexed but missing disk: {len(stale_indexed)} (stale DB rows)")

    if without_profile:
        print()
        print("== files missing profile (by extension) ==")
        ext_counts = Counter(Path(p).suffix.lower() or "<none>" for p in without_profile)
        for ext, n in ext_counts.most_common():
            print(f"  {ext:12s} {n}")

        show = int(__import__("os").environ.get("AUDIT_SHOW_N", "30"))
        print()
        print(f"== first {show} files missing profile ==")
        for p in without_profile[:show]:
            print(f"  {p}")
        if len(without_profile) > show:
            print(f"  ... and {len(without_profile) - show} more")

    if stale_indexed:
        print()
        print("== stale DB rows (indexed path no longer on disk) ==")
        for p in stale_indexed[:20]:
            print(f"  {p}  -> profile={index.get(p, '')!r}")
        if len(stale_indexed) > 20:
            print(f"  ... and {len(stale_indexed) - 20} more")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

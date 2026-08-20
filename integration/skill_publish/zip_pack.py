"""One-shot zip packaging for skill publish submissions (docs §7.5.1).

Zips are disposable transport artifacts: created in the system temp dir via
mkstemp (prefix ``hermes-skill-publish-``), streamed to upstream, and
unlinked in a ``finally``. Never write them into the skills dir or STATE_DIR.
Startup sweep removes crash leftovers (finally never ran).
"""

from __future__ import annotations

import logging
import os
import tempfile
import zipfile
from pathlib import Path

_log = logging.getLogger(__name__)

TEMP_PREFIX = "hermes-skill-publish-"
TEMP_SUFFIX = ".zip"

# Hidden marker/metadata files stay local; never ship them upstream.
_EXCLUDED_NAMES = {".user_created", ".detail.json", ".category", ".hub_catalog_name"}


def build_skill_zip(skill_dir: Path | str, dest: Path | str) -> int:
    """Recursively zip ``skill_dir`` into ``dest``. Returns entry count.

    Excludes hidden files (dotfiles) - they are local markers/metadata, not
    skill content (docs §7.5.1 打包内容).
    """
    src = Path(skill_dir)
    count = 0
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        for fp in sorted(src.rglob("*")):
            rel = fp.relative_to(src)
            if any(part.startswith(".") for part in rel.parts):
                continue
            if fp.name in _EXCLUDED_NAMES:
                continue
            if fp.is_file():
                zf.write(fp, arcname=rel.as_posix())
                count += 1
    return count


def create_temp_zip_path() -> Path:
    """Allocate a unique temp zip path; caller must close fd and unlink."""
    fd, path = tempfile.mkstemp(prefix=TEMP_PREFIX, suffix=TEMP_SUFFIX)
    os.close(fd)
    return Path(path)


def safe_unlink(path: Path | str | None) -> None:
    if not path:
        return
    try:
        os.unlink(path)
    except OSError:
        pass


def sweep_stale_publish_zips() -> int:
    """Remove crash-leftover publish zips from the system temp dir (startup).

    Multi-instance caveat (docs §7.5.1): only safe when instances do not share
    a temp dir; default container deployments are isolated.
    """
    removed = 0
    try:
        targets = list(Path(tempfile.gettempdir()).glob(f"{TEMP_PREFIX}*{TEMP_SUFFIX}"))
    except OSError:
        return 0
    for p in targets:
        try:
            p.unlink()
            removed += 1
        except OSError:
            _log.warning("skill-publish: failed to sweep stale zip %s", p)
    return removed

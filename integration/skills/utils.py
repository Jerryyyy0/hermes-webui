"""Shared skill filesystem helpers."""

from __future__ import annotations

import io
import logging
import shutil
import zipfile
from pathlib import Path

_log = logging.getLogger(__name__)

_SYSTEM_SKILL_NAMES = frozenset({"hermes", "default"})


def is_system_skill(name: str) -> bool:
    base = (name or "").strip().split("/")[-1].lower()
    return base in _SYSTEM_SKILL_NAMES


def skill_path_within(base_dir: Path, candidate: Path) -> bool:
    try:
        candidate.resolve().relative_to(base_dir.resolve())
        return True
    except (OSError, ValueError):
        return False


def find_skill_main_file(skill_dir: Path) -> Path | None:
    skill_md = skill_dir / "SKILL.md"
    if skill_md.is_file():
        return skill_md
    legacy = skill_dir.with_suffix(".md")
    if legacy.is_file():
        return legacy
    return None


def has_hub_installed_marker(skill_dir: Path, skills_root: Path) -> bool:
    """True when skill_dir or any ancestor up to skills_root carries .hub_installed.

    Normally the marker sits next to SKILL.md. A zip whose wrapper layer was
    not flattened leaves the marker on an ancestor while SKILL.md nests one
    level deeper, so checks must walk up instead of looking at skill_dir only.
    """
    node = skill_dir
    while True:
        if (node / ".hub_installed").is_file():
            return True
        if node == skills_root or node.parent == node:
            break
        node = node.parent
    return False


def skill_uninstall_root(skill_dir: Path, skills_root: Path) -> Path:
    """Directory to remove on uninstall.

    The install unit is the directory carrying ``.hub_installed``. When that
    dir wraps the real skill content (skillId/skill-name/SKILL.md, e.g.
    admin-assigned layouts), the whole wrapper must go so no marker-bearing
    shell is left behind. Wrappers holding more than one skill keep their
    other skills: only the requested skill_dir is removed.
    """
    node = skill_dir
    while node != skills_root and node.parent != node:
        if (node / ".hub_installed").is_file():
            if len(list(node.rglob("SKILL.md"))) <= 1:
                return node
            return skill_dir
        node = node.parent
    return skill_dir


def find_skill_main_dir(base: Path) -> Path | None:
    """Resolve the directory whose direct child SKILL.md is the skill's main file.

    Normally that is ``base`` itself. When the install wrapper was not
    flattened (e.g. a hash-named skillId dir wrapping the real skill folder),
    descend through the unambiguous single-dir chain until a directory holding
    SKILL.md is found. Ambiguous layouts fall back to the shallowest SKILL.md
    under ``base``. Returns None when no SKILL.md exists below ``base`` at all.
    """
    if not base.is_dir():
        return None
    node = base
    while True:
        if find_skill_main_file(node):
            return node
        children = [
            child
            for child in node.iterdir()
            if child.is_dir() and not child.name.startswith(".")
        ]
        if len(children) != 1:
            break
        node = children[0]
    hits = sorted(
        (skill_md for skill_md in base.rglob("SKILL.md") if skill_md.is_file()),
        key=lambda skill_md: (len(skill_md.parts), skill_md.as_posix()),
    )
    return hits[0].parent if hits else None


def _promote_nested_skill_dir(target_dir: Path) -> None:
    """Lift a single nested skill dir up to target_dir.

    SkillHub zips bundle a manifest file (skill.json) at the archive root next
    to the actual skill folder, which defeats the common-prefix flattening above
    and leaves SKILL.md one level below the install sidecars. When target_dir
    itself has no SKILL.md and exactly one immediate child does, move that
    child's contents up (nested copy wins on conflicts).
    """
    if find_skill_main_file(target_dir):
        return
    candidates = [
        child
        for child in target_dir.iterdir()
        if child.is_dir()
        and not child.name.startswith(".")
        and find_skill_main_file(child)
    ]
    if len(candidates) != 1:
        return
    nested = candidates[0]
    for entry in list(nested.iterdir()):
        dest = target_dir / entry.name
        if dest.exists():
            if dest.is_dir():
                shutil.rmtree(dest)
            else:
                dest.unlink()
        entry.rename(dest)
    nested.rmdir()


def extract_zip_and_flatten(zip_bytes: bytes, target_dir: Path) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        members = [m for m in zf.namelist() if m and not m.endswith("/")]
        if not members:
            raise ValueError("压缩包为空")
        common_prefix = ""
        if len(members) > 1:
            parts = [m.split("/") for m in members]
            prefix = []
            for segment in zip(*parts):
                if len(set(segment)) == 1:
                    prefix.append(segment[0])
                else:
                    break
            if prefix:
                common_prefix = "/".join(prefix) + "/"
        for member in members:
            rel = member
            if common_prefix and rel.startswith(common_prefix):
                rel = rel[len(common_prefix) :]
            if not rel or rel.startswith("__MACOSX"):
                continue
            dest = (target_dir / rel).resolve()
            if not skill_path_within(target_dir, dest):
                raise ValueError("压缩包路径非法")
            if member.endswith("/"):
                dest.mkdir(parents=True, exist_ok=True)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(zf.read(member))
    _promote_nested_skill_dir(target_dir)


def stream_zip_to_handler(
    handler,
    zip_name: str,
    files: list[tuple[Path, str]],
    extra_entries: list[tuple[bytes, str]] | None = None,
) -> None:
    """Stream a zip archive to the HTTP handler response body."""
    from api.routes import _content_disposition_value

    handler.send_response(200)
    handler.send_header("Content-Type", "application/zip")
    handler.send_header(
        "Content-Disposition",
        _content_disposition_value("attachment", zip_name),
    )
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Connection", "close")
    handler.end_headers()

    written = 0
    with zipfile.ZipFile(
        handler.wfile, mode="w", compression=zipfile.ZIP_DEFLATED, allowZip64=True
    ) as zf:
        for fp, arcname in files:
            try:
                zf.write(fp, arcname=arcname)
                written += 1
            except (OSError, PermissionError) as exc:
                _log.warning("skill-download: skipping %s: %s", fp, exc)
        for content, arcname in extra_entries or []:
            zf.writestr(arcname, content)
            written += 1
    _log.debug("skill-download: streamed %d/%d files as %s", written, len(files), zip_name)

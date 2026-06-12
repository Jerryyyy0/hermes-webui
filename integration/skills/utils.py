"""Shared skill filesystem helpers."""

from __future__ import annotations

import io
import logging
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

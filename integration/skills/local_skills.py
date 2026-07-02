"""Local skills directory scan and CRUD."""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from pathlib import Path

from integration.skills.paths import shared_skills_dir, skills_dir_for_profile
from integration.skills.utils import (
    extract_zip_and_flatten,
    find_skill_main_file,
    is_system_skill,
    skill_path_within,
)
from integration.skills.validate import validate_skill_md_content
from integration.skills.zip_import import discover_skill_roots

_log = logging.getLogger(__name__)

_SKILL_META_EXCLUDE = frozenset(
    {".hub_installed", ".category", ".install_name", ".hub_catalog_name"}
)
_SKILL_ORIGIN_SIDECAR = ".skill-origin.json"
_UPLOAD_COPY_EXCLUDE = _SKILL_META_EXCLUDE | {_SKILL_ORIGIN_SIDECAR}


def _skill_zip_max_bytes() -> int:
    try:
        mb = int(os.getenv("HERMES_WEBUI_FOLDER_ZIP_MAX_MB", "1024"))
    except ValueError:
        mb = 1024
    return max(1, mb) * 1024 * 1024


def _skill_zip_max_files() -> int:
    try:
        return max(1, int(os.getenv("HERMES_WEBUI_FOLDER_ZIP_MAX_FILES", "50000")))
    except ValueError:
        return 50000


def collect_skill_zip_files(
    skill_dir: Path,
    max_bytes: int,
    max_files: int,
    *,
    arc_prefix: str = "",
) -> tuple[list[tuple[Path, str]], int, str | None]:
    """Walk skill_dir; return (files, total_bytes, limit_hit). Excludes integration metadata."""
    files: list[tuple[Path, str]] = []
    total_bytes = 0
    skill_root = skill_dir.resolve()
    prefix = str(arc_prefix or "").strip().strip("/")
    for root, _dirs, names in os.walk(skill_root, followlinks=False):
        root_path = Path(root)
        try:
            if not root_path.resolve().is_relative_to(skill_root):
                continue
        except (ValueError, OSError):
            continue
        for name in names:
            if name in _SKILL_META_EXCLUDE:
                continue
            fp = root_path / name
            if fp.is_symlink():
                try:
                    if not fp.resolve().is_relative_to(skill_root):
                        continue
                except (ValueError, OSError):
                    continue
            try:
                size = fp.stat().st_size
            except OSError:
                continue
            if len(files) >= max_files:
                return files, total_bytes, "max_files"
            if total_bytes + size > max_bytes:
                return files, total_bytes, "max_bytes"
            try:
                arcname = fp.relative_to(skill_root).as_posix()
            except ValueError:
                continue
            if prefix:
                arcname = f"{prefix}/{arcname}"
            files.append((fp, arcname))
            total_bytes += size
    return files, total_bytes, None


def prepare_skill_download(name: str, dir_name: str = "") -> dict:
    """Resolve a local skill directory and collect files for zip download."""
    skill_name = str(name or "").strip()
    if not skill_name:
        return {"error": "缺少 name", "status": 400}
    if is_system_skill(skill_name):
        return {"error": "Cannot download system skill", "status": 403}

    skills_dir = shared_skills_dir()
    skill_dir = _resolve_skill_dir(skills_dir, skill_name, dir_name)
    if not skill_dir or not skill_dir.is_dir():
        return {"error": "Skill not found", "status": 404}

    max_bytes = _skill_zip_max_bytes()
    max_files = _skill_zip_max_files()
    skill_md = find_skill_main_file(skill_dir)
    list_name = _list_name_from_skill_md(skill_md, skill_dir.name) if skill_md else skill_dir.name
    leaf = normalize_dir_name(list_name)
    if validate_dir_name(leaf) is not None:
        leaf = skill_dir.name
    dir_name = _skill_dir_rel_path(skill_dir, skills_dir)
    stored_category = _stored_category_for_dir(skill_dir, skills_dir, "")
    files, total_bytes, limit_hit = collect_skill_zip_files(
        skill_dir,
        max_bytes,
        max_files,
        arc_prefix=leaf,
    )
    if limit_hit == "max_files":
        return {
            "error": "too many files",
            "status": 413,
            "limit": max_files,
            "configure": "HERMES_WEBUI_FOLDER_ZIP_MAX_FILES",
        }
    if limit_hit == "max_bytes":
        return {
            "error": "skill too large",
            "status": 413,
            "limit_bytes": max_bytes,
            "configure": "HERMES_WEBUI_FOLDER_ZIP_MAX_MB",
        }

    zip_basename = f"{leaf}.zip"
    sidecar_payload = {
        "version": 1,
        "dir_name": dir_name,
        "category": stored_category,
        "name": list_name,
    }
    sidecar_arcname = f"{leaf}/{_SKILL_ORIGIN_SIDECAR}"
    sidecar_bytes = (json.dumps(sidecar_payload, ensure_ascii=False, indent=2) + "\n").encode(
        "utf-8"
    )
    return {
        "ok": True,
        "skill_dir": skill_dir,
        "zip_basename": zip_basename,
        "files": files,
        "extra_zip_entries": [(sidecar_bytes, sidecar_arcname)],
        "total_bytes": total_bytes,
    }


def list_installed(
    profile: str,
    category: str | None = None,
) -> dict:
    from agent.skill_utils import iter_skill_index_files
    from tools.skills_tool import (
        MAX_DESCRIPTION_LENGTH,
        _EXCLUDED_SKILL_DIRS,
        _get_disabled_skill_names,
        _parse_frontmatter,
        _sort_skills,
        skill_matches_platform,
    )

    skills_dir = skills_dir_for_profile(profile)
    if not skills_dir.exists():
        skills_dir.mkdir(parents=True, exist_ok=True)
        return {
            "skills": [],
            "skillhub_enabled": False,
            "categories": [],
            "total": 0,
        }

    disabled = _get_disabled_skill_names()
    all_skills: list[dict] = []
    seen: set[str] = set()

    for skill_md in iter_skill_index_files(skills_dir, "SKILL.md"):
        if any(part in _EXCLUDED_SKILL_DIRS for part in skill_md.parts):
            continue
        skill_dir = skill_md.parent
        try:
            rel = skill_md.relative_to(skills_dir)
            parts = rel.parts
            if len(parts) >= 3:
                full_name = "/".join(parts[:-1])
                cat = parts[0]
            elif len(parts) == 2:
                full_name = parts[0]
                cat = parts[0]
            else:
                full_name = skill_dir.name
                cat = None
            content = skill_md.read_text(encoding="utf-8")[:4000]
            frontmatter, body = _parse_frontmatter(content)
            if not skill_matches_platform(frontmatter):
                continue
            name = str(frontmatter.get("name", skill_dir.name))[:64]
            if name in seen:
                continue
            description = str(frontmatter.get("description", "") or "")
            if not description:
                for line in body.strip().split("\n"):
                    line = line.strip()
                    if line and not line.startswith("#"):
                        description = line
                        break
            if len(description) > MAX_DESCRIPTION_LENGTH:
                description = description[: MAX_DESCRIPTION_LENGTH - 3] + "..."
            seen.add(name)
            hub_installed = (skill_dir / ".hub_installed").is_file()
            install_name = ""
            install_file = skill_dir / ".install_name"
            if install_file.is_file():
                install_name = install_file.read_text(encoding="utf-8").strip()
            all_skills.append(
                {
                    "name": name,
                    "full_name": full_name,
                    "description": description,
                    "category": cat,
                    "version": str(frontmatter.get("version", "") or ""),
                    "author": str(frontmatter.get("author", "") or ""),
                    "hub_installed": hub_installed,
                    "can_delete": not is_system_skill(name),
                    "install_name": install_name or name,
                    "installed": True,
                    "disabled": name in disabled,
                }
            )
        except Exception as exc:
            _log.debug("skip skill %s: %s", skill_md, exc)

    if category:
        all_skills = [s for s in all_skills if s.get("category") == category]
    all_skills = _sort_skills(all_skills)
    try:
        from integration.skills.no_self_improve import apply_lock_fields_batch

        apply_lock_fields_batch(all_skills)
    except Exception:
        for skill in all_skills:
            hub = bool(skill.get("hub_installed"))
            skill.setdefault("no_self_improve", hub)
            skill.setdefault("can_lock", not hub)
    categories = sorted({s.get("category") for s in all_skills if s.get("category")})
    return {
        "skills": all_skills,
        "skillhub_enabled": False,
        "categories": categories,
        "total": len(all_skills),
    }


def _scan_custom_skill_dicts(
    category: str,
    hub_names: set[str],
    q: str | None = None,
) -> list[dict]:
    from agent.skill_utils import iter_skill_index_files
    from tools.skills_tool import (
        MAX_DESCRIPTION_LENGTH,
        _EXCLUDED_SKILL_DIRS,
        _get_disabled_skill_names,
        _parse_frontmatter,
        skill_matches_platform,
    )

    skills_dir = shared_skills_dir()
    if not skills_dir.exists():
        skills_dir.mkdir(parents=True, exist_ok=True)
    disabled = _get_disabled_skill_names()
    all_skills: list[dict] = []
    seen: set[str] = set()
    query = str(q or "").strip().lower()

    for skill_md in iter_skill_index_files(skills_dir, "SKILL.md"):
        if any(part in _EXCLUDED_SKILL_DIRS for part in skill_md.parts):
            continue
        skill_dir = skill_md.parent
        try:
            rel = skill_md.relative_to(skills_dir)
            parts = rel.parts
            if len(parts) >= 3:
                cat = parts[0]
            elif len(parts) == 2:
                cat = parts[0]
            else:
                cat = None
            category_file = skill_dir / ".category"
            if category_file.is_file():
                cat = category_file.read_text(encoding="utf-8").strip() or cat
            if category and cat != category:
                continue
            content = skill_md.read_text(encoding="utf-8")[:4000]
            frontmatter, body = _parse_frontmatter(content)
            if not skill_matches_platform(frontmatter):
                continue
            name = str(frontmatter.get("name", skill_dir.name))[:64]
            dir_key = _skill_dir_rel_path(skill_dir, skills_dir)
            if dir_key in seen:
                continue
            # Exclude hub-installed skills: check marker file first (reliable),
            # then fall back to catalog name matching.
            if (skill_dir / ".hub_installed").is_file():
                continue
            if name in hub_names:
                continue
            description = str(frontmatter.get("description", "") or "")
            if not description:
                for line in body.strip().split("\n"):
                    line = line.strip()
                    if line and not line.startswith("#"):
                        description = line
                        break
            if len(description) > MAX_DESCRIPTION_LENGTH:
                description = description[: MAX_DESCRIPTION_LENGTH - 3] + "..."
            if query:
                haystack = " ".join(
                    [
                        name,
                        str(frontmatter.get("display_name", "") or ""),
                        description,
                    ]
                ).lower()
                if query not in haystack:
                    continue
            seen.add(dir_key)
            entry: dict = {
                "name": name,
                "dir_name": dir_key,
                "display_name": str(frontmatter.get("display_name", "") or ""),
                "description": description,
                "category": str(cat or ""),
                "version": str(frontmatter.get("version", "") or ""),
                "author": str(frontmatter.get("author", "") or ""),
                "installed": True,
                "hub_installed": False,
                "custom": True,
                "disabled": name in disabled,
            }
            try:
                entry["mtime"] = float(skill_md.stat().st_mtime)
            except OSError:
                entry["mtime"] = None
            all_skills.append(entry)
        except Exception as exc:
            _log.debug("skip skill %s: %s", skill_md, exc)

    try:
        from integration.skills.no_self_improve import apply_lock_fields_batch

        apply_lock_fields_batch(all_skills)
    except Exception:
        for skill in all_skills:
            skill.setdefault("no_self_improve", False)
            skill.setdefault("can_lock", True)
    return all_skills


def scan_custom_skills_global(hub_names: set[str], q: str | None = None) -> list[dict]:
    """Scan all custom skills under shared_skills_dir (no category filter)."""
    return _scan_custom_skill_dicts("", hub_names, q=q)


def _filter_custom_skills_in_memory(
    skills: list[dict],
    category: str,
    q: str | None,
) -> list[dict]:
    category_key = str(category or "").strip()
    filtered = skills
    if category_key:
        filtered = [skill for skill in filtered if str(skill.get("category") or "") == category_key]
    query = str(q or "").strip().lower()
    if not query:
        return filtered
    result: list[dict] = []
    for skill in filtered:
        haystack = " ".join(
            [
                str(skill.get("name") or ""),
                str(skill.get("display_name") or ""),
                str(skill.get("description") or ""),
            ]
        ).lower()
        if query in haystack:
            result.append(skill)
    return result


def count_custom_skills(category: str, hub_names: set[str]) -> int:
    return len(_scan_custom_skill_dicts(category, hub_names))


def list_custom_skills(
    category: str,
    hub_names: set[str],
    q: str | None = None,
    page: int = 1,
    page_size: int = 20,
    sort: str = "name",
    order: str = "asc",
    all_records: bool = False,
    pre_scanned: list[dict] | None = None,
) -> dict:
    from integration.skills.list_item_shape import normalize_skill_list_items
    from integration.skills.sort_utils import sort_skill_items

    if pre_scanned is not None:
        all_skills = _filter_custom_skills_in_memory(pre_scanned, category, q)
    else:
        all_skills = _scan_custom_skill_dicts(category, hub_names, q=q)
    all_skills = sort_skill_items(all_skills, sort=sort, order=order)
    total = len(all_skills)
    if all_records:
        page_items = normalize_skill_list_items(all_skills)
        page_num = 1
        page_limit = total
    else:
        offset = (page - 1) * page_size
        page_items = normalize_skill_list_items(all_skills[offset : offset + page_size])
        page_num = page
        page_limit = page_size
    return {
        "scope": "custom",
        "category": category,
        "skills": page_items,
        "total": total,
        "page": page_num,
        "page_size": page_limit,
        "skillhub_enabled": True,
    }


def _find_skill(name: str, skills_dir: Path) -> tuple[Path | None, Path | None]:
    from agent.skill_utils import iter_skill_index_files
    from tools.skills_tool import _EXCLUDED_SKILL_DIRS, _parse_frontmatter

    raw = str(name or "").strip().strip("/")
    if not raw:
        return None, None
    candidates = [raw]
    if "/" not in raw and ":" in raw:
        ns, bare = raw.split(":", 1)
        if ns and bare:
            candidates.append(f"{ns}/{bare}")

    for candidate in candidates:
        direct = skills_dir / candidate
        if skill_path_within(skills_dir, direct):
            if direct.is_dir():
                main = find_skill_main_file(direct)
                if main:
                    return direct, main
            legacy = direct.with_suffix(".md")
            if legacy.is_file():
                return legacy.parent, legacy

    for skill_md in iter_skill_index_files(skills_dir, "SKILL.md"):
        if any(part in _EXCLUDED_SKILL_DIRS for part in skill_md.parts):
            continue
        skill_dir = skill_md.parent
        if skill_dir.name == raw:
            return skill_dir, skill_md
        try:
            fm, _ = _parse_frontmatter(skill_md.read_text(encoding="utf-8")[:4000])
            if fm.get("name") == raw:
                return skill_dir, skill_md
        except Exception:
            continue
    return None, None


def has_local_skill(name: str) -> bool:
    """True when name resolves to SKILL.md under shared_skills_dir."""
    skills_dir = shared_skills_dir()
    _, skill_md = _find_skill(name, skills_dir)
    return skill_md is not None


def _structure_file_entries(skill_dir: Path, subdir: str, extensions: list[str]) -> list[dict]:
    folder = skill_dir / subdir
    if not folder.exists():
        return []
    paths: set[str] = set()
    for ext in extensions:
        for file_path in folder.rglob(ext):
            if file_path.is_file():
                paths.add(str(file_path.relative_to(skill_dir)))
    return [{"path": p} for p in sorted(paths)]


def get_custom_doc(name: str) -> dict:
    skills_dir = shared_skills_dir()
    skill_dir, skill_md = _find_skill(name, skills_dir)
    if not skill_md:
        return {"error": "Skill not found", "status": 404}
    return {
        "name": name,
        "content": skill_md.read_text(encoding="utf-8"),
        "linked_files": {},
    }


def get_custom_structure(name: str) -> dict:
    skills_dir = shared_skills_dir()
    skill_dir, skill_md = _find_skill(name, skills_dir)
    if not skill_dir or not skill_md:
        return {"error": "Skill not found", "status": 404}
    return {
        "name": name,
        "scripts": _structure_file_entries(
            skill_dir,
            "scripts",
            ["*.py", "*.sh", "*.bash", "*.js", "*.ts", "*.rb"],
        ),
        "references": _structure_file_entries(skill_dir, "references", ["*.md"]),
    }


def get_custom_file(name: str, file_path: str) -> dict:
    skills_dir = shared_skills_dir()
    skill_dir, skill_md = _find_skill(name, skills_dir)
    if not skill_dir or not skill_md:
        return {"error": "Skill not found", "status": 404}
    target = (skill_dir / file_path).resolve()
    if not skill_path_within(skill_dir, target) or not target.is_file():
        return {"error": "File not found", "status": 404}
    return {
        "name": name,
        "path": file_path,
        "content": target.read_text(encoding="utf-8"),
    }


def get_content(profile: str, name: str, file_path: str = "") -> dict:
    skills_dir = skills_dir_for_profile(profile)
    skill_dir, skill_md = _find_skill(name, skills_dir)
    if not skill_md:
        return {"error": "Skill not found", "status": 404}
    if file_path:
        target = (skill_dir / file_path).resolve()
        if not skill_path_within(skill_dir, target) or not target.is_file():
            return {"error": "File not found", "status": 404}
        return {"content": target.read_text(encoding="utf-8"), "linked_files": {}}
    return {
        "content": skill_md.read_text(encoding="utf-8"),
        "linked_files": {},
    }


def save_skill(profile: str, name: str, content: str, category: str = "") -> dict:
    skill_name = name.strip().lower().replace(" ", "-")
    if not skill_name or "/" in skill_name or ".." in skill_name:
        return {"error": "Invalid skill name", "status": 400}
    if category and ("/" in category or ".." in category):
        return {"error": "Invalid category", "status": 400}
    skills_dir = skills_dir_for_profile(profile)
    if category:
        skill_dir = skills_dir / category / skill_name
    else:
        skill_dir = skills_dir / skill_name
    try:
        skill_dir.resolve().relative_to(skills_dir.resolve())
    except ValueError:
        return {"error": "Invalid skill path", "status": 400}
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(content, encoding="utf-8")
    if category:
        (skill_dir / ".category").write_text(category, encoding="utf-8")
    return {"ok": True, "name": skill_name, "path": str(skill_file)}


def normalize_dir_name(raw: str) -> str:
    return str(raw or "").strip().lower().replace(" ", "-")[:64]


def validate_dir_name(dir_name: str) -> dict | None:
    if not dir_name or "/" in dir_name or ".." in dir_name:
        return {"error": "无效的技能名称", "status": 400}
    if is_system_skill(dir_name):
        return {"error": "无效的技能名称", "status": 400}
    return None


def parse_logical_name_from_content(content: str) -> str | None:
    """Parse frontmatter ``name`` from SKILL.md content."""
    try:
        from tools.skills_tool import _parse_frontmatter

        frontmatter, _ = _parse_frontmatter(str(content or "")[:4000])
        raw = frontmatter.get("name")
        if raw is not None and str(raw).strip():
            return str(raw).strip()[:64]
    except Exception:
        pass
    return None


def parse_logical_name_from_skill_md(skill_md: Path) -> str | None:
    try:
        return parse_logical_name_from_content(skill_md.read_text(encoding="utf-8"))
    except Exception as exc:
        _log.debug("parse_logical_name_from_skill_md failed for %s: %s", skill_md, exc)
        return None


def find_custom_skill_dirs_by_name(skills_dir: Path, logical_name: str) -> list[Path]:
    """Return custom skill directories whose frontmatter name matches logical_name."""
    from agent.skill_utils import iter_skill_index_files
    from tools.skills_tool import _EXCLUDED_SKILL_DIRS, _parse_frontmatter

    key = str(logical_name or "").strip()
    if not key:
        return []
    matches: list[Path] = []
    seen_paths: set[str] = set()
    for skill_md in iter_skill_index_files(skills_dir, "SKILL.md"):
        if any(part in _EXCLUDED_SKILL_DIRS for part in skill_md.parts):
            continue
        skill_dir = skill_md.parent
        if (skill_dir / ".hub_installed").is_file():
            continue
        try:
            frontmatter, _ = _parse_frontmatter(skill_md.read_text(encoding="utf-8")[:4000])
            name = str(frontmatter.get("name", skill_dir.name)).strip()[:64]
        except Exception:
            name = skill_dir.name
        if name != key:
            continue
        path_key = skill_dir.resolve().as_posix()
        if path_key in seen_paths:
            continue
        seen_paths.add(path_key)
        matches.append(skill_dir)
    return matches


def read_skill_origin_sidecar(skill_root: Path) -> dict | None:
    path = skill_root / _SKILL_ORIGIN_SIDECAR
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception as exc:
        _log.debug("read_skill_origin_sidecar failed for %s: %s", skill_root, exc)
        return None


def _resolve_dir_from_explicit(skills_dir: Path, explicit_dir_name: str) -> tuple[Path | None, dict | None]:
    dir_key = str(explicit_dir_name or "").strip().strip("/")
    if not dir_key or ".." in dir_key:
        return None, {"error": "无效的技能路径", "status": 400}
    candidate = (skills_dir / dir_key).resolve()
    if not skill_path_within(skills_dir, candidate):
        return None, {"error": "无效的技能路径", "status": 400}
    return candidate, None


def _stored_category_for_dir(skill_dir: Path, skills_dir: Path, request_category: str) -> str:
    category_file = skill_dir / ".category"
    if category_file.is_file():
        cat = category_file.read_text(encoding="utf-8").strip()
        if cat:
            return cat
    rel = _skill_dir_rel_path(skill_dir, skills_dir)
    parts = rel.split("/")
    if len(parts) >= 2:
        return parts[0]
    cat_seg, _ = _normalize_category_segment(request_category)
    return cat_seg or ""


def resolve_upload_target(
    skills_dir: Path,
    *,
    category: str,
    leaf: str,
    logical_name: str,
    explicit_dir_name: str = "",
    sidecar: dict | None = None,
    overwrite: bool = False,
) -> dict | tuple[Path, str, list[Path]]:
    """Resolve upload destination and directories to remove before write."""
    matches = find_custom_skill_dirs_by_name(skills_dir, logical_name)
    if any((path / ".hub_installed").is_file() for path in matches):
        return {"error": "市场安装的技能不可覆盖", "status": 409}

    target: Path | None = None
    stored_category = ""

    explicit = str(explicit_dir_name or "").strip()
    if explicit:
        target, path_err = _resolve_dir_from_explicit(skills_dir, explicit)
        if path_err:
            return path_err
        assert target is not None
        stored_category = _stored_category_for_dir(target, skills_dir, category)
    else:
        sidecar_dir = str((sidecar or {}).get("dir_name") or "").strip()
        if sidecar_dir:
            target, path_err = _resolve_dir_from_explicit(skills_dir, sidecar_dir)
            if path_err:
                return path_err
            assert target is not None
            sidecar_cat = str((sidecar or {}).get("category") or "").strip()
            stored_category = sidecar_cat or _stored_category_for_dir(target, skills_dir, category)
        elif len(matches) == 1:
            target = matches[0]
            stored_category = _stored_category_for_dir(target, skills_dir, category)
        elif len(matches) > 1:
            default_dest, cat_seg, path_err = skill_target_dir(skills_dir, category, leaf)
            if path_err:
                return path_err
            assert default_dest is not None
            match_resolved = {path.resolve() for path in matches}
            if default_dest.resolve() in match_resolved:
                target = default_dest
            else:
                target = sorted(matches, key=lambda p: _skill_dir_rel_path(p, skills_dir))[0]
            stored_category = _stored_category_for_dir(target, skills_dir, category)
        else:
            target, cat_seg, path_err = skill_target_dir(skills_dir, category, leaf)
            if path_err:
                return path_err
            assert target is not None and cat_seg is not None
            stored_category = cat_seg or ""

    assert target is not None
    dirs_to_remove: list[Path] = []
    if overwrite:
        dirs_to_remove = list(matches)
        if _target_conflict(target):
            if _hub_installed_target(target):
                return {"error": "市场安装的技能不可覆盖", "status": 409}
            if target.resolve() not in {path.resolve() for path in matches}:
                dirs_to_remove.append(target)
    else:
        if matches:
            return {"error": "技能已存在", "status": 409}
        if _target_conflict(target):
            if _hub_installed_target(target):
                return {"error": "市场安装的技能不可覆盖", "status": 409}
            return {"error": "技能已存在", "status": 409}

    return target, stored_category, dirs_to_remove


def _remove_upload_dirs(paths: list[Path]) -> None:
    seen: set[str] = set()
    for path in paths:
        key = path.resolve().as_posix()
        if key in seen or not path.exists():
            continue
        seen.add(key)
        shutil.rmtree(path, ignore_errors=True)


def _copy_skill_tree(src: Path, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    def _ignore(_dir: str, names: list[str]) -> set[str]:
        return {name for name in names if name in _UPLOAD_COPY_EXCLUDE}

    shutil.copytree(src, dest, ignore=_ignore)


def resolve_dir_name(request_name: str, filename: str | None) -> tuple[str | None, dict | None]:
    """Return (dir_name, error_payload)."""
    explicit = str(request_name or "").strip()
    if explicit:
        dir_name = normalize_dir_name(explicit)
        err = validate_dir_name(dir_name)
        return (None, err) if err else (dir_name, None)
    if filename:
        stem = Path(filename).stem
        dir_name = normalize_dir_name(stem)
        err = validate_dir_name(dir_name)
        return (None, err) if err else (dir_name, None)
    return (None, {"error": "缺少 name", "status": 400})


def _list_name_from_skill_md(skill_md: Path, dir_name: str) -> str:
    try:
        content = skill_md.read_text(encoding="utf-8")[:4000]
        try:
            from tools.skills_tool import _parse_frontmatter

            frontmatter, _ = _parse_frontmatter(content)
            raw = frontmatter.get("name")
            if raw is not None and str(raw).strip():
                return str(raw).strip()[:64]
        except Exception:
            if content.startswith("---"):
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    for line in parts[1].splitlines():
                        stripped = line.strip()
                        if stripped.lower().startswith("name:"):
                            val = stripped.split(":", 1)[1].strip().strip('"').strip("'")
                            if val:
                                return val[:64]
    except Exception as exc:
        _log.debug("list_name parse failed for %s: %s", skill_md, exc)
    return dir_name


def _validate_category(category: str) -> dict | None:
    if category and ("/" in category or ".." in category):
        return {"error": "无效的分类", "status": 400}
    return None


def _normalize_category_segment(category: str) -> tuple[str | None, dict | None]:
    """Normalize upload/list category to a single path segment."""
    raw = str(category or "").strip()
    if not raw:
        return "", None
    seg = normalize_dir_name(raw)
    if not seg:
        return None, {"error": "无效的分类", "status": 400}
    err = validate_dir_name(seg)
    if err:
        return None, {"error": "无效的分类", "status": 400}
    return seg, None


def skill_target_dir(
    skills_dir: Path, category: str, leaf_name: str
) -> tuple[Path | None, str | None, dict | None]:
    """Resolve install/upload destination; empty category → flat skills/<leaf>/."""
    leaf = normalize_dir_name(leaf_name)
    leaf_err = validate_dir_name(leaf)
    if leaf_err:
        return None, None, leaf_err
    cat_seg, cat_err = _normalize_category_segment(category)
    if cat_err:
        return None, None, cat_err
    assert cat_seg is not None
    if cat_seg:
        target = skills_dir / cat_seg / leaf
    else:
        target = skills_dir / leaf
    try:
        target.resolve().relative_to(skills_dir.resolve())
    except ValueError:
        return None, None, {"error": "无效的技能路径", "status": 400}
    return target, cat_seg, None


def _upload_target_dir(
    skills_dir: Path, dir_name: str, category: str
) -> tuple[Path | None, dict | None]:
    target, _, path_err = skill_target_dir(skills_dir, category, dir_name)
    return target, path_err


def _write_category_marker(skill_dir: Path, cat_seg: str) -> None:
    if cat_seg:
        (skill_dir / ".category").write_text(cat_seg, encoding="utf-8")


def _skill_upload_entry(
    skill_dir: Path,
    skills_dir: Path,
    list_name: str,
    stored_category: str,
) -> dict:
    return {
        "name": list_name,
        "dir_name": _skill_dir_rel_path(skill_dir, skills_dir),
        "category": stored_category,
        "custom": True,
    }


def _count_files_recursive(root: Path) -> int:
    if not root.is_dir():
        return 0
    return sum(1 for path in root.rglob("*") if path.is_file())


def _upload_batch_response(entries: list[dict], *, file_count: int) -> dict:
    return {
        "ok": True,
        "skill_count": len(entries),
        "file_count": file_count,
        "skills": entries,
    }


def _target_conflict(target: Path) -> bool:
    if (target / ".hub_installed").is_file():
        return True
    return find_skill_main_file(target) is not None


def _hub_installed_target(target: Path) -> bool:
    return (target / ".hub_installed").is_file()


def _leaf_from_skill_root(skill_root: Path, skill_md: Path) -> str:
    from tools.skills_tool import _parse_frontmatter

    try:
        fm, _ = _parse_frontmatter(skill_md.read_text(encoding="utf-8")[:4000])
        raw = fm.get("name")
        if raw is not None and str(raw).strip():
            leaf = normalize_dir_name(str(raw).strip())
            if validate_dir_name(leaf) is None:
                return leaf
    except Exception:
        pass
    return skill_root.name


def _plan_zip_import(
    skills_dir: Path,
    category: str,
    roots: list[Path],
    *,
    overwrite: bool = False,
    explicit_dir_name: str = "",
) -> tuple[list[tuple[Path, Path, str, str, list[Path]]], list[str], int]:
    """Return (planned copies, error messages, http_status_if_errors)."""
    cat_seg, cat_err = _normalize_category_segment(category)
    if cat_err:
        return [], [str(cat_err["error"])], int(cat_err.get("status") or 400)

    planned: list[tuple[Path, Path, str, str, list[Path]]] = []
    errors: list[str] = []
    status = 409
    seen_dests: set[str] = set()

    for skill_root in roots:
        skill_md = find_skill_main_file(skill_root)
        if not skill_md:
            errors.append(f"{skill_root.name}: 缺少 SKILL.md")
            status = 400
            continue
        content = skill_md.read_text(encoding="utf-8")
        fmt_err = validate_skill_md_content(content)
        if fmt_err:
            errors.append(f"{skill_root.name}: {fmt_err.get('error', 'SKILL.md 格式无效')}")
            status = 400
            continue
        leaf = _leaf_from_skill_root(skill_root, skill_md)
        leaf_err = validate_dir_name(leaf)
        if leaf_err:
            errors.append(f"{skill_root.name}: {leaf_err.get('error', '名称无效')}")
            status = 400
            continue
        logical_name = parse_logical_name_from_content(content) or leaf
        sidecar = read_skill_origin_sidecar(skill_root)
        resolved = resolve_upload_target(
            skills_dir,
            category=category,
            leaf=leaf,
            logical_name=logical_name,
            explicit_dir_name=explicit_dir_name,
            sidecar=sidecar,
            overwrite=overwrite,
        )
        if isinstance(resolved, dict):
            errors.append(f"{leaf}: {resolved.get('error', '上传失败')}")
            if int(resolved.get("status") or 0) == 400:
                status = 400
            continue
        dest, stored_category, dirs_to_remove = resolved
        dest_key = dest.resolve().as_posix()
        if dest_key in seen_dests:
            errors.append(f"{leaf}: 压缩包内重复")
            status = 409
            continue
        seen_dests.add(dest_key)
        list_name = _list_name_from_skill_md(skill_md, leaf)
        planned.append((skill_root, dest, list_name, stored_category, dirs_to_remove))

    return planned, errors, status


def _upload_zip_skills(
    skills_dir: Path,
    category: str,
    zip_bytes: bytes,
    *,
    overwrite: bool = False,
    explicit_dir_name: str = "",
) -> dict:
    temp_dir = Path(tempfile.mkdtemp(prefix="hermes-skill-upload-"))
    created: list[Path] = []
    ok = False
    try:
        extract_zip_and_flatten(zip_bytes, temp_dir)
        roots = discover_skill_roots(temp_dir)
        if not roots:
            return {"error": "压缩包内需包含 SKILL.md", "status": 400}

        planned, errors, err_status = _plan_zip_import(
            skills_dir,
            category,
            roots,
            overwrite=overwrite,
            explicit_dir_name=explicit_dir_name,
        )
        if errors:
            return {"error": "; ".join(errors), "status": err_status}

        entries: list[dict] = []
        for skill_root, dest, list_name, stored_category, dirs_to_remove in planned:
            _remove_upload_dirs(dirs_to_remove)
            _copy_skill_tree(skill_root, dest)
            created.append(dest)
            _write_category_marker(dest, stored_category)
            entries.append(_skill_upload_entry(dest, skills_dir, list_name, stored_category))

        ok = True
        file_count = sum(_count_files_recursive(skill_root) for skill_root, _, _, _, _ in planned)
        return _upload_batch_response(entries, file_count=file_count)
    except ValueError as exc:
        return {"error": str(exc), "status": 400}
    except Exception as exc:
        _log.debug("upload zip failed: %s", exc)
        return {"error": "上传失败", "status": 500}
    finally:
        if not ok:
            for path in created:
                shutil.rmtree(path, ignore_errors=True)
        shutil.rmtree(temp_dir, ignore_errors=True)


def _upload_single_md(
    skills_dir: Path,
    category: str,
    content: str,
    leaf: str,
    *,
    overwrite: bool = False,
    explicit_dir_name: str = "",
) -> dict:
    fmt_err = validate_skill_md_content(content)
    if fmt_err:
        return fmt_err
    logical_name = parse_logical_name_from_content(content) or leaf
    resolved = resolve_upload_target(
        skills_dir,
        category=category,
        leaf=leaf,
        logical_name=logical_name,
        explicit_dir_name=explicit_dir_name,
        sidecar=None,
        overwrite=overwrite,
    )
    if isinstance(resolved, dict):
        return resolved
    target, stored_category, dirs_to_remove = resolved

    created = False
    ok = False
    try:
        _remove_upload_dirs(dirs_to_remove)
        target.mkdir(parents=True, exist_ok=True)
        created = True
        (target / "SKILL.md").write_text(content, encoding="utf-8")
        skill_md = find_skill_main_file(target)
        if not skill_md:
            return {"error": "缺少 SKILL.md", "status": 400}
        fmt_err = validate_skill_md_content(skill_md.read_text(encoding="utf-8"))
        if fmt_err:
            return fmt_err
        _write_category_marker(target, stored_category)
        list_name = _list_name_from_skill_md(skill_md, leaf)
        ok = True
        return _upload_batch_response(
            [_skill_upload_entry(target, skills_dir, list_name, stored_category)],
            file_count=1,
        )
    except Exception as exc:
        _log.debug("upload md failed: %s", exc)
        return {"error": "上传失败", "status": 500}
    finally:
        if created and not ok:
            shutil.rmtree(target, ignore_errors=True)


def upload_custom_skill(
    *,
    request_name: str = "",
    category: str = "",
    content: str | None = None,
    zip_bytes: bytes | None = None,
    filename: str | None = None,
    overwrite: bool = False,
    explicit_dir_name: str = "",
) -> dict:
    """Write custom skill(s) to shared_skills_dir(); no upstream SkillHub calls."""
    category = str(category or "").strip()
    cat_err = _validate_category(category)
    if cat_err:
        return cat_err

    if zip_bytes is not None and content is not None:
        return {"error": "不能同时提供文件内容与 zip", "status": 400}
    if zip_bytes is None and content is None:
        return {"error": "缺少技能内容", "status": 400}

    skills_dir = shared_skills_dir()
    skills_dir.mkdir(parents=True, exist_ok=True)
    explicit = str(explicit_dir_name or "").strip()

    if zip_bytes is not None:
        return _upload_zip_skills(
            skills_dir,
            category,
            zip_bytes,
            overwrite=overwrite,
            explicit_dir_name=explicit,
        )

    assert content is not None
    fmt_err = validate_skill_md_content(content)
    if fmt_err:
        return fmt_err
    logical_name = parse_logical_name_from_content(content)
    if logical_name:
        leaf = normalize_dir_name(logical_name)
        leaf_err = validate_dir_name(leaf)
        if leaf_err:
            return leaf_err
    else:
        leaf, name_err = resolve_dir_name(request_name, filename)
        if name_err:
            return name_err
        assert leaf is not None

    return _upload_single_md(
        skills_dir,
        category,
        content,
        leaf,
        overwrite=overwrite,
        explicit_dir_name=explicit,
    )


def _skill_dir_rel_path(skill_dir: Path, skills_dir: Path) -> str:
    """Relative path from skills root to the skill directory (e.g. apple/apple-notes)."""
    try:
        rel = skill_dir.resolve().relative_to(skills_dir.resolve())
        if not rel.parts or rel == Path("."):
            return skill_dir.name
        return rel.as_posix()
    except ValueError:
        return skill_dir.name


def _resolve_skill_dir(skills_dir: Path, name: str, dir_name: str = "") -> Path | None:
    dir_key = str(dir_name or "").strip()
    if dir_key:
        candidate = (skills_dir / dir_key).resolve()
        if skill_path_within(skills_dir, candidate) and candidate.is_dir():
            return candidate
    skill_dir, _ = _find_skill(name, skills_dir)
    return skill_dir


def edit_custom_skill(*, name: str, content: str, dir_name: str = "") -> dict:
    """Update SKILL.md for an existing custom skill in shared_skills_dir."""
    skill_name = str(name or "").strip()
    if not skill_name:
        return {"error": "缺少 name", "status": 400}
    if content is None or (isinstance(content, str) and not content.strip()):
        return {"error": "缺少 content", "status": 400}
    if is_system_skill(skill_name):
        return {"error": "Cannot edit system skill", "status": 403}

    skills_dir = shared_skills_dir()
    skill_dir = _resolve_skill_dir(skills_dir, skill_name, dir_name)
    if not skill_dir:
        return {"error": "Skill not found", "status": 404}
    if (skill_dir / ".hub_installed").is_file():
        return {"error": "市场安装的技能不可编辑", "status": 403}

    fmt_err = validate_skill_md_content(content)
    if fmt_err:
        return fmt_err

    skill_md = find_skill_main_file(skill_dir)
    if not skill_md:
        return {"error": "Skill not found", "status": 404}

    skill_md.write_text(content, encoding="utf-8")
    list_name = _list_name_from_skill_md(skill_md, skill_dir.name)
    stored_category = ""
    category_file = skill_dir / ".category"
    if category_file.is_file():
        stored_category = category_file.read_text(encoding="utf-8").strip()

    return {
        "ok": True,
        "name": list_name,
        "dir_name": _skill_dir_rel_path(skill_dir, skills_dir),
        "category": stored_category,
        "custom": True,
    }


def delete_local_skill(name: str, dir_name: str = "") -> dict:
    """Remove a hub install or custom skill from shared_skills_dir."""
    if is_system_skill(name):
        return {"error": "Cannot delete system skill", "status": 403}
    skills_dir = shared_skills_dir()
    skill_dir = _resolve_skill_dir(skills_dir, name, dir_name)
    if not skill_dir:
        return {"error": "Skill not found", "status": 404}
    hub_installed = (skill_dir / ".hub_installed").is_file()
    skill_md = find_skill_main_file(skill_dir)
    logical_name = (
        parse_logical_name_from_skill_md(skill_md) if skill_md else None
    ) or str(name or "").strip() or skill_dir.name
    shutil.rmtree(skill_dir)
    try:
        from integration.skills.no_self_improve import remove_names

        remove_names([logical_name])
    except Exception as exc:
        _log.exception("failed to remove skill from no_self_improve: %s", exc)
    return {
        "ok": True,
        "name": logical_name,
        "dir_name": _skill_dir_rel_path(skill_dir, skills_dir),
        "hub_installed": hub_installed,
    }

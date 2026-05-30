"""Local skills directory scan and CRUD."""

from __future__ import annotations

import logging
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
        _sort_skills,
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
            if name in seen:
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
            seen.add(name)
            all_skills.append(
                {
                    "name": name,
                    "dir_name": _skill_dir_rel_path(skill_dir, skills_dir),
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
            )
        except Exception as exc:
            _log.debug("skip skill %s: %s", skill_md, exc)

    return _sort_skills(all_skills)


def count_custom_skills(category: str, hub_names: set[str]) -> int:
    return len(_scan_custom_skill_dicts(category, hub_names))


def list_custom_skills(
    category: str,
    hub_names: set[str],
    q: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> dict:
    all_skills = _scan_custom_skill_dicts(category, hub_names, q=q)
    total = len(all_skills)
    offset = (page - 1) * page_size
    page_items = all_skills[offset : offset + page_size]
    return {
        "scope": "custom",
        "category": category,
        "skills": page_items,
        "total": total,
        "page": page,
        "page_size": page_size,
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
) -> tuple[list[tuple[Path, Path, str, str]], list[str], int]:
    """Return (planned copies, error messages, http_status_if_errors)."""
    cat_seg, cat_err = _normalize_category_segment(category)
    if cat_err:
        return [], [str(cat_err["error"])], int(cat_err.get("status") or 400)

    assert cat_seg is not None
    stored_category = cat_seg or ""
    planned: list[tuple[Path, Path, str, str]] = []
    errors: list[str] = []
    status = 409
    seen_dests: set[str] = set()

    for skill_root in roots:
        skill_md = find_skill_main_file(skill_root)
        if not skill_md:
            errors.append(f"{skill_root.name}: 缺少 SKILL.md")
            status = 400
            continue
        fmt_err = validate_skill_md_content(skill_md.read_text(encoding="utf-8"))
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
        dest, _, path_err = skill_target_dir(skills_dir, category, leaf)
        if path_err:
            errors.append(f"{leaf}: {path_err.get('error', '路径无效')}")
            status = 400
            continue
        assert dest is not None
        dest_key = dest.resolve().as_posix()
        if dest_key in seen_dests:
            errors.append(f"{leaf}: 压缩包内重复")
            status = 409
            continue
        seen_dests.add(dest_key)
        if _target_conflict(dest):
            errors.append(f"{leaf}: 技能已存在")
            continue
        list_name = _list_name_from_skill_md(skill_md, leaf)
        planned.append((skill_root, dest, list_name, stored_category))

    return planned, errors, status


def _upload_zip_skills(
    skills_dir: Path,
    category: str,
    zip_bytes: bytes,
) -> dict:
    temp_dir = Path(tempfile.mkdtemp(prefix="hermes-skill-upload-"))
    created: list[Path] = []
    ok = False
    try:
        extract_zip_and_flatten(zip_bytes, temp_dir)
        roots = discover_skill_roots(temp_dir)
        if not roots:
            return {"error": "压缩包内需包含 SKILL.md", "status": 400}

        planned, errors, err_status = _plan_zip_import(skills_dir, category, roots)
        if errors:
            return {"error": "; ".join(errors), "status": err_status}

        entries: list[dict] = []
        for skill_root, dest, list_name, stored_category in planned:
            shutil.copytree(skill_root, dest)
            created.append(dest)
            _write_category_marker(dest, stored_category)
            entries.append(_skill_upload_entry(dest, skills_dir, list_name, stored_category))

        ok = True
        file_count = sum(_count_files_recursive(skill_root) for skill_root, _, _, _ in planned)
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
    dir_name: str,
) -> dict:
    target, cat_seg, path_err = skill_target_dir(skills_dir, category, dir_name)
    if path_err:
        return path_err
    assert target is not None and cat_seg is not None

    if _target_conflict(target):
        return {"error": "技能已存在", "status": 409}

    created = False
    ok = False
    stored_category = cat_seg or ""
    try:
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
        list_name = _list_name_from_skill_md(skill_md, dir_name)
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

    if zip_bytes is not None:
        return _upload_zip_skills(skills_dir, category, zip_bytes)

    dir_name, name_err = resolve_dir_name(request_name, filename)
    if name_err:
        return name_err
    assert dir_name is not None and content is not None
    return _upload_single_md(skills_dir, category, content, dir_name)


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


def delete_local_skill(name: str, dir_name: str = "") -> dict:
    """Remove a hub install or custom skill from shared_skills_dir."""
    if is_system_skill(name):
        return {"error": "Cannot delete system skill", "status": 403}
    skills_dir = shared_skills_dir()
    skill_dir = _resolve_skill_dir(skills_dir, name, dir_name)
    if not skill_dir:
        return {"error": "Skill not found", "status": 404}
    hub_installed = (skill_dir / ".hub_installed").is_file()
    removed_name = str(name or "").strip() or skill_dir.name
    shutil.rmtree(skill_dir)
    return {
        "ok": True,
        "name": removed_name,
        "dir_name": _skill_dir_rel_path(skill_dir, skills_dir),
        "hub_installed": hub_installed,
    }

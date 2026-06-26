"""SkillHub upstream HTTP client and install."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from integration.config import skillhub_url
from integration.skills.local_skills import _skill_dir_rel_path, skill_target_dir
from integration.skills.list_item_shape import normalize_skill_list_items
from integration.skills.mtime_utils import enrich_skills_mtime
from integration.skills.paths import shared_skills_dir
from integration.skills.sort_utils import sort_skill_items
from integration.skills.utils import (
    extract_zip_and_flatten,
    find_skill_main_file,
    skill_path_within,
)

_log = logging.getLogger(__name__)
_TIMEOUT = 30.0


def _client() -> httpx.Client:
    return httpx.Client(timeout=_TIMEOUT, follow_redirects=True)


def _hub_base() -> str:
    url = skillhub_url()
    if not url:
        raise RuntimeError("SKILLHUB_URL not configured")
    return url


def _skill_path(name: str) -> str:
    return quote(str(name or "").strip(), safe="")


def fetch_categories() -> list[str]:
    with _client() as client:
        resp = client.get(f"{_hub_base()}/api/skills/categories")
        resp.raise_for_status()
        data = resp.json()
    if isinstance(data, list):
        return [str(x) for x in data if x]
    return []


def fetch_catalog(
    q: str | None = None,
    category: str | None = None,
    page: int | None = None,
    page_size: int | None = None,
) -> dict:
    params: dict[str, Any] = {}
    if q:
        params["q"] = q
    if category:
        params["category"] = category
    if page is not None:
        params["page"] = page
    if page_size is not None:
        params["page_size"] = page_size

    with _client() as client:
        resp = client.get(f"{_hub_base()}/api/skills", params=params)
        resp.raise_for_status()
        data = resp.json()

    skills = data.get("skills") if isinstance(data, dict) else data
    if not isinstance(skills, list):
        skills = []
    mapped = [dict(s) for s in skills if isinstance(s, dict)]
    result: dict[str, Any] = {
        "skills": mapped,
        "total": data.get("total", len(mapped)) if isinstance(data, dict) else len(mapped),
    }
    if isinstance(data, dict):
        if data.get("page") is not None:
            result["page"] = data["page"]
        if data.get("page_size") is not None:
            result["page_size"] = data["page_size"]
    return result


def fetch_skill_detail(name: str) -> dict:
    with _client() as client:
        resp = client.get(f"{_hub_base()}/api/skills/{_skill_path(name)}")
        resp.raise_for_status()
        data = resp.json()
    return data if isinstance(data, dict) else {"name": name}


def fetch_doc(name: str) -> dict:
    with _client() as client:
        resp = client.get(f"{_hub_base()}/api/skills/{_skill_path(name)}/doc")
        resp.raise_for_status()
        if resp.headers.get("content-type", "").startswith("application/json"):
            payload = resp.json()
            if isinstance(payload, dict):
                content = payload.get("content") or payload.get("readme") or ""
                return {
                    "name": payload.get("name") or name,
                    "content": content,
                    "linked_files": {},
                }
        return {"name": name, "content": resp.text, "linked_files": {}}


def fetch_structure(name: str) -> dict:
    with _client() as client:
        resp = client.get(f"{_hub_base()}/api/skills/{_skill_path(name)}/structure")
        resp.raise_for_status()
        data = resp.json()
    return data if isinstance(data, dict) else {"name": name, "scripts": [], "references": []}


def fetch_file(name: str, path: str) -> dict:
    with _client() as client:
        resp = client.get(
            f"{_hub_base()}/api/skills/{_skill_path(name)}/file",
            params={"path": path},
        )
        resp.raise_for_status()
        if resp.headers.get("content-type", "").startswith("application/json"):
            data = resp.json()
            if isinstance(data, dict):
                return data
        return {"name": name, "path": path, "content": resp.text}


def download_bytes(name: str) -> bytes:
    with _client() as client:
        resp = client.get(f"{_hub_base()}/api/skills/{_skill_path(name)}/download")
        resp.raise_for_status()
        return resp.content


def _read_local_skill_description(skills_dir: Path, dir_name: str) -> str:
    """Read description from local SKILL.md when installed under dir_name."""
    rel = str(dir_name or "").strip()
    if not rel:
        return ""
    candidate = (skills_dir / rel).resolve()
    if not skill_path_within(skills_dir, candidate) or not candidate.is_dir():
        return ""
    skill_md = find_skill_main_file(candidate)
    if not skill_md or not skill_md.is_file():
        return ""
    try:
        from tools.skills_tool import MAX_DESCRIPTION_LENGTH, _parse_frontmatter

        content = skill_md.read_text(encoding="utf-8")[:4000]
        frontmatter, body = _parse_frontmatter(content)
        description = str(frontmatter.get("description", "") or "").strip()
        if not description:
            for line in body.strip().split("\n"):
                line = line.strip()
                if line and not line.startswith("#"):
                    description = line
                    break
        description = str(description or "").strip()
        if not description:
            return ""
        if len(description) > MAX_DESCRIPTION_LENGTH:
            description = description[: MAX_DESCRIPTION_LENGTH - 3] + "..."
        return description
    except Exception:
        return ""


def _read_skill_catalog_name(skill_dir: Path, leaf: str) -> str:
    skill_md = find_skill_main_file(skill_dir)
    if not skill_md:
        return leaf
    try:
        from tools.skills_tool import _parse_frontmatter

        frontmatter, _ = _parse_frontmatter(skill_md.read_text(encoding="utf-8")[:4000])
        raw = frontmatter.get("name")
        if raw is not None and str(raw).strip():
            return str(raw).strip()[:64]
    except Exception:
        pass
    return leaf


def _disabled_skill_names() -> set[str]:
    try:
        from tools.skills_tool import _get_disabled_skill_names

        return _get_disabled_skill_names()
    except Exception:
        return set()


def _hub_installed_index(skills_dir: Path) -> dict[str, str]:
    """Map catalog skill name to relative path under skills_dir for hub installs."""
    index: dict[str, str] = {}
    if not skills_dir.exists():
        return index
    for marker in skills_dir.rglob(".hub_installed"):
        if not marker.is_file():
            continue
        skill_dir = marker.parent
        leaf = skill_dir.name
        if not leaf or leaf.startswith("."):
            continue
        catalog_name = _read_skill_catalog_name(skill_dir, leaf)
        dir_name = _skill_dir_rel_path(skill_dir, skills_dir)
        index[catalog_name] = dir_name
        if leaf != catalog_name:
            index.setdefault(leaf, dir_name)
    return index


def annotate_installed(
    skills: list[dict],
    *,
    installed_index: dict[str, str] | None = None,
    locked_names: set[str] | None = None,
) -> list[dict]:
    """Mark hub catalog items with local install state under shared_skills_dir."""
    skills_dir = shared_skills_dir()
    if installed_index is None:
        try:
            installed_index = _hub_installed_index(skills_dir)
        except Exception as exc:
            _log.debug("annotate_installed failed: %s", exc)
            installed_index = {}

    disabled = _disabled_skill_names()
    lock_fields_ok = True
    if locked_names is None:
        try:
            from integration.skills.no_self_improve import get_no_self_improve_names

            locked_names = get_no_self_improve_names()
        except Exception:
            lock_fields_ok = False
            locked_names = set()

    for skill in skills:
        skill_name = str(skill.get("name") or "").strip()
        dir_name = installed_index.get(skill_name, "")
        is_installed = bool(dir_name)
        skill["installed"] = is_installed
        skill["hub_installed"] = is_installed
        skill["custom"] = False
        skill["dir_name"] = dir_name
        skill["disabled"] = skill_name in disabled
        if is_installed and dir_name:
            local_description = _read_local_skill_description(skills_dir, dir_name)
            if local_description:
                skill["description"] = local_description
        skill.pop("catalog_only", None)
        if lock_fields_ok:
            try:
                from integration.skills.no_self_improve import apply_lock_fields

                apply_lock_fields(skill, locked_names)
            except Exception:
                skill["no_self_improve"] = is_installed
                skill["can_lock"] = False
        else:
            skill["no_self_improve"] = is_installed
            skill["can_lock"] = False
    return skills


def fetch_all_hub_skills(category: str | None = None) -> list[dict]:
    """Fetch the full upstream catalog for one category (or all categories when empty)."""
    all_skills: list[dict] = []
    page = 1
    page_size = 100
    cat_param = category if category else None
    while True:
        payload = fetch_catalog(category=cat_param, page=page, page_size=page_size)
        skills = payload.get("skills") or []
        all_skills.extend(dict(s) for s in skills if isinstance(s, dict))
        total = int(payload.get("total") or 0)
        if not skills or len(all_skills) >= total:
            break
        page += 1
    return all_skills


@dataclass
class _HubCatalogContext:
    raw_skills: list[dict]
    hub_names: set[str]
    installed_index: dict[str, str]
    annotated_all: list[dict]
    locked_names: set[str]


def _hub_names_from_skills(skills: list[dict]) -> set[str]:
    names: set[str] = set()
    for skill in skills:
        name = str(skill.get("name") or "").strip()
        if name:
            names.add(name)
    return names


def build_hub_catalog_context() -> _HubCatalogContext:
    """Fetch hub catalog once per request and precompute install annotations."""
    raw_skills = fetch_all_hub_skills(category=None)
    hub_names = _hub_names_from_skills(raw_skills)
    skills_dir = shared_skills_dir()
    try:
        installed_index = _hub_installed_index(skills_dir)
    except Exception as exc:
        _log.debug("build_hub_catalog_context installed index failed: %s", exc)
        installed_index = {}
    try:
        from integration.skills.no_self_improve import get_no_self_improve_names

        locked_names = get_no_self_improve_names()
    except Exception:
        locked_names = set()
    annotated = [dict(skill) for skill in raw_skills]
    annotate_installed(
        annotated,
        installed_index=installed_index,
        locked_names=locked_names,
    )
    return _HubCatalogContext(
        raw_skills=raw_skills,
        hub_names=hub_names,
        installed_index=installed_index,
        annotated_all=annotated,
        locked_names=locked_names,
    )


def compute_scope_stats_from(ctx: _HubCatalogContext, *, custom_count: int) -> dict[str, int]:
    """Global scope tab counts from a prebuilt hub catalog context."""
    hub_count = len(ctx.annotated_all)
    installed_count = sum(1 for skill in ctx.annotated_all if skill.get("installed"))
    return {
        "hub": hub_count,
        "installed": installed_count,
        "not_installed": hub_count - installed_count,
        "custom": custom_count,
    }


def _filter_skills_by_category(skills: list[dict], category: str) -> list[dict]:
    category_key = str(category or "").strip()
    if not category_key:
        return skills
    return [skill for skill in skills if str(skill.get("category") or "") == category_key]


def list_hub_catalog_filtered_from(
    ctx: _HubCatalogContext,
    category: str,
    scope: str,
    q: str | None,
    sort: str = "name",
    order: str = "asc",
) -> tuple[list[dict], int]:
    """List hub catalog items from a prebuilt context (no extra upstream fetch)."""
    skills = _filter_skills_by_category(ctx.annotated_all, category)
    if scope == "installed":
        skills = [skill for skill in skills if skill.get("installed")]
    elif scope == "not_installed":
        skills = [skill for skill in skills if not skill.get("installed")]
    skills = _filter_skills_by_q(skills, q)
    if sort == "mtime":
        skills = enrich_skills_mtime([dict(skill) for skill in skills], shared_skills_dir())
    skills = sort_skill_items(skills, sort=sort, order=order)
    skills = normalize_skill_list_items(skills)
    total = len(skills)
    return skills, total


def list_hub_catalog_paged_from(
    ctx: _HubCatalogContext,
    category: str,
    scope: str,
    q: str | None,
    page: int,
    page_size: int,
    sort: str = "name",
    order: str = "asc",
) -> tuple[list[dict], int]:
    skills, total = list_hub_catalog_filtered_from(
        ctx,
        category,
        scope,
        q,
        sort=sort,
        order=order,
    )
    offset = (page - 1) * page_size
    return skills[offset : offset + page_size], total


def _filter_skills_by_q(skills: list[dict], q: str | None) -> list[dict]:
    query = str(q or "").strip().lower()
    if not query:
        return skills
    filtered: list[dict] = []
    for skill in skills:
        haystack = " ".join(
            [
                str(skill.get("name") or ""),
                str(skill.get("display_name") or ""),
                str(skill.get("description") or ""),
            ]
        ).lower()
        if query in haystack:
            filtered.append(skill)
    return filtered


def compute_scope_stats(hub_names: set[str]) -> dict[str, int]:
    """Global scope tab counts (all categories; ignores list q/category filters)."""
    from integration.skills import local_skills

    ctx = build_hub_catalog_context()
    custom_count = local_skills.count_custom_skills("", hub_names)
    return compute_scope_stats_from(ctx, custom_count=custom_count)


def list_hub_catalog_filtered(
    category: str,
    scope: str,
    q: str | None,
    sort: str = "name",
    order: str = "asc",
) -> tuple[list[dict], int]:
    """List hub catalog items with local filter and sort (no pagination)."""
    ctx = build_hub_catalog_context()
    return list_hub_catalog_filtered_from(
        ctx,
        category,
        scope,
        q,
        sort=sort,
        order=order,
    )


def list_hub_catalog_paged(
    category: str,
    scope: str,
    q: str | None,
    page: int,
    page_size: int,
    sort: str = "name",
    order: str = "asc",
) -> tuple[list[dict], int]:
    """List hub catalog items with local filter, sort, and pagination."""
    skills, total = list_hub_catalog_filtered(
        category,
        scope,
        q,
        sort=sort,
        order=order,
    )
    offset = (page - 1) * page_size
    return skills[offset : offset + page_size], total


def list_hub_skills_filtered(
    category: str,
    scope: str,
    q: str | None,
    page: int,
    page_size: int,
    sort: str = "name",
    order: str = "asc",
) -> tuple[list[dict], int]:
    """Backward-compatible alias for list_hub_catalog_paged."""
    return list_hub_catalog_paged(
        category,
        scope,
        q,
        page,
        page_size,
        sort=sort,
        order=order,
    )


def hub_all_catalog_names() -> set[str]:
    """Aggregate every skill name from the upstream catalog (all categories)."""
    return build_hub_catalog_context().hub_names


def install_skill(name: str, display_name: str = "", category: str = "") -> dict:
    skills_dir = shared_skills_dir()
    skills_dir.mkdir(parents=True, exist_ok=True)
    target, cat_seg, path_err = skill_target_dir(skills_dir, category, name)
    if path_err:
        return path_err
    assert target is not None and cat_seg is not None

    if find_skill_main_file(target) or (target / ".hub_installed").is_file():
        return {"error": "Skill already installed", "status": 409}

    label = (display_name or name).strip()
    try:
        zip_bytes = download_bytes(name)
        target.mkdir(parents=True, exist_ok=True)
        extract_zip_and_flatten(zip_bytes, target)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 404:
            raise
        doc = fetch_doc(name)
        text = str(doc.get("content") or "")
        target.mkdir(parents=True, exist_ok=True)
        (target / "SKILL.md").write_text(text, encoding="utf-8")
    except Exception:
        doc = fetch_doc(name)
        text = str(doc.get("content") or "")
        target.mkdir(parents=True, exist_ok=True)
        (target / "SKILL.md").write_text(text, encoding="utf-8")

    if cat_seg:
        (target / ".category").write_text(cat_seg, encoding="utf-8")
    (target / ".hub_installed").write_text("1", encoding="utf-8")
    (target / ".install_name").write_text(label, encoding="utf-8")
    try:
        from integration.skills.no_self_improve import add_names

        add_names([name])
    except Exception as exc:
        _log.exception("failed to add hub skill to no_self_improve: %s", exc)
    return {
        "ok": True,
        "name": name,
        "category": cat_seg or "",
        "dir_name": _skill_dir_rel_path(target, skills_dir),
    }


# Backward-compatible alias for tests/callers
fetch_catalog_content = fetch_doc

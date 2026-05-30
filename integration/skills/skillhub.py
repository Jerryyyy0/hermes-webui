"""SkillHub upstream HTTP client and install."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from integration.config import skillhub_url
from integration.skills.local_skills import _skill_dir_rel_path, skill_target_dir
from integration.skills.paths import shared_skills_dir
from integration.skills.utils import extract_zip_and_flatten, find_skill_main_file

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


def annotate_installed(skills: list[dict]) -> list[dict]:
    """Mark hub catalog items with local install state under shared_skills_dir."""
    installed_index: dict[str, str] = {}
    try:
        installed_index = _hub_installed_index(shared_skills_dir())
    except Exception as exc:
        _log.debug("annotate_installed failed: %s", exc)

    disabled = _disabled_skill_names()

    for skill in skills:
        skill_name = str(skill.get("name") or "").strip()
        dir_name = installed_index.get(skill_name, "")
        is_installed = bool(dir_name)
        skill["installed"] = is_installed
        skill["hub_installed"] = is_installed
        skill["custom"] = False
        skill["dir_name"] = dir_name
        skill["disabled"] = skill_name in disabled
        skill.pop("catalog_only", None)
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

    hub_skills = annotate_installed(fetch_all_hub_skills(category=None))
    hub_count = len(hub_skills)
    installed_count = sum(1 for skill in hub_skills if skill.get("installed"))
    return {
        "hub": hub_count,
        "installed": installed_count,
        "not_installed": hub_count - installed_count,
        "custom": local_skills.count_custom_skills("", hub_names),
    }


def list_hub_skills_filtered(
    category: str,
    scope: str,
    q: str | None,
    page: int,
    page_size: int,
) -> tuple[list[dict], int]:
    """List hub catalog items filtered by installed state, with local pagination."""
    cat_param = category if category else None
    skills = annotate_installed(fetch_all_hub_skills(cat_param))
    if scope == "installed":
        skills = [skill for skill in skills if skill.get("installed")]
    elif scope == "not_installed":
        skills = [skill for skill in skills if not skill.get("installed")]
    skills = _filter_skills_by_q(skills, q)
    total = len(skills)
    offset = (page - 1) * page_size
    return skills[offset : offset + page_size], total


def hub_all_catalog_names() -> set[str]:
    """Aggregate every skill name from the upstream catalog (all categories)."""
    names: set[str] = set()
    page = 1
    page_size = 100
    while True:
        payload = fetch_catalog(page=page, page_size=page_size)
        skills = payload.get("skills") or []
        for skill in skills:
            if not isinstance(skill, dict):
                continue
            name = str(skill.get("name") or "").strip()
            if name:
                names.add(name)
        total = int(payload.get("total") or 0)
        if not skills or len(names) >= total:
            break
        page += 1
    return names


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
    return {
        "ok": True,
        "name": name,
        "category": cat_seg or "",
        "dir_name": _skill_dir_rel_path(target, skills_dir),
    }


# Backward-compatible alias for tests/callers
fetch_catalog_content = fetch_doc

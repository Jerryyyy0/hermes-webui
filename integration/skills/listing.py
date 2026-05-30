"""SkillHub catalog listing (upstream proxy + local custom skills)."""

from __future__ import annotations

from integration.skills import local_skills, skillhub

_VALID_SCOPES = frozenset({"hub", "installed", "not_installed", "custom"})


def _normalize_page(page: int | None) -> int:
    if page is None or page < 1:
        return 1
    return page


def _normalize_page_size(page_size: int | None) -> int:
    if page_size is None or page_size < 1:
        return 20
    return min(page_size, 100)


def _normalize_scope(scope: str | None) -> str:
    scope_key = str(scope or "hub").strip().lower()
    if scope_key not in _VALID_SCOPES:
        return "hub"
    return scope_key


def _envelope(
    *,
    scope: str,
    category: str,
    skills: list[dict],
    total: int,
    page: int,
    page_size: int,
    stats: dict[str, int],
) -> dict:
    return {
        "scope": scope,
        "category": category,
        "skills": skills,
        "total": total,
        "page": page,
        "page_size": page_size,
        "skillhub_enabled": True,
        "stats": stats,
    }


def list_skillhub_skills(
    category: str = "",
    scope: str = "hub",
    q: str | None = None,
    page: int | None = None,
    page_size: int | None = None,
) -> dict:
    scope_key = _normalize_scope(scope)
    page_num = _normalize_page(page)
    page_limit = _normalize_page_size(page_size)
    category_key = str(category or "").strip()
    hub_names = skillhub.hub_all_catalog_names()
    stats = skillhub.compute_scope_stats(hub_names)

    if scope_key == "custom":
        payload = local_skills.list_custom_skills(
            category=category_key,
            q=q,
            hub_names=hub_names,
            page=page_num,
            page_size=page_limit,
        )
        payload["stats"] = stats
        return payload

    if scope_key in ("installed", "not_installed"):
        skills, total = skillhub.list_hub_skills_filtered(
            category=category_key,
            scope=scope_key,
            q=q,
            page=page_num,
            page_size=page_limit,
        )
        return _envelope(
            scope=scope_key,
            category=category_key,
            skills=skills,
            total=total,
            page=page_num,
            page_size=page_limit,
            stats=stats,
        )

    cat_param = category_key if category_key else None
    payload = skillhub.fetch_catalog(
        q=q,
        category=cat_param,
        page=page_num,
        page_size=page_limit,
    )
    skills = skillhub.annotate_installed(payload.get("skills") or [])
    return _envelope(
        scope="hub",
        category=category_key,
        skills=skills,
        total=payload.get("total", len(skills)),
        page=payload.get("page", page_num),
        page_size=payload.get("page_size", page_limit),
        stats=stats,
    )

"""SkillHub catalog listing (upstream proxy + local custom skills)."""

from __future__ import annotations

from integration.skills import local_skills, skillhub
from integration.skills.list_item_shape import normalize_skill_list_items
from integration.skills.mtime_utils import enrich_skills_mtime
from integration.skills.paths import skills_dir_for_profile
from integration.skills.sort_utils import sort_skill_items
from integration.skills.skillhub import _filter_skills_by_category, _filter_skills_by_q, _is_uncategorized_match

_VALID_SCOPES = frozenset({"hub", "installed", "not_installed", "custom", "local_all"})


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


def _normalize_profile(profile: str | None) -> str:
    return str(profile or "default").strip() or "default"


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


def _local_all_skills_for_profile(
    ctx: skillhub._HubCatalogContext,
    profile: str,
) -> tuple[list[dict], list[dict]]:
    """Installed hub + custom skills under a single profile's skills dir."""
    profile_key = _normalize_profile(profile)
    skills_dir = skills_dir_for_profile(profile_key)
    disabled = skillhub._disabled_skill_names_for_profile(profile_key)
    installed_index = skillhub._hub_installed_index(skills_dir)
    annotated = [dict(skill) for skill in ctx.raw_skills]
    skillhub.annotate_installed(
        annotated,
        installed_index=installed_index,
        index_profile=profile_key,
        locked_names=ctx.locked_names,
        disabled_names=disabled,
    )
    installed_hub = [skill for skill in annotated if skill.get("installed")]
    custom_skills = [
        dict(skill)
        for skill in local_skills._scan_custom_skill_dicts(
            skills_dir, "", user_created_only=True
        )
    ]
    for skill in custom_skills:
        name = str(skill.get("name") or "").strip()
        skill["disabled"] = name in disabled
    return installed_hub, custom_skills


def list_skillhub_skills(
    category: str = "",
    scope: str = "hub",
    profile: str = "default",
    q: str | None = None,
    page: int | None = None,
    page_size: int | None = None,
    sort: str = "name",
    order: str = "asc",
    all_records: bool = False,
) -> dict:
    scope_key = _normalize_scope(scope)
    page_num = _normalize_page(page)
    page_limit = _normalize_page_size(page_size)
    category_key = str(category or "").strip()
    profile_key = _normalize_profile(profile)
    ctx = skillhub.build_hub_catalog_context()
    # Always scan user-created skills for stats and custom scope
    custom_all = local_skills.scan_custom_skills_global(
        ctx.hub_names, profile=profile_key, user_created_only=True
    )
    stats = skillhub.compute_scope_stats_from(ctx, custom_count=len(custom_all))

    if scope_key == "custom":
        payload = local_skills.list_custom_skills(
            category=category_key,
            profile=profile_key,
            q=q,
            hub_names=ctx.hub_names,
            page=page_num,
            page_size=page_limit,
            sort=sort,
            order=order,
            all_records=all_records,
            pre_scanned=custom_all,
        )
        payload["stats"] = stats
        return payload

    if scope_key == "local_all":
        # profile filters only this scope: scan that profile's skills dir
        installed_hub, custom_skills = _local_all_skills_for_profile(ctx, profile_key)
        custom_names = {str(s.get("name") or "").strip() for s in custom_skills}
        merged = list(custom_skills) + [
            s
            for s in installed_hub
            if str(s.get("name") or "").strip() not in custom_names
        ]
        merged = [skill for skill in merged if not skill.get("disabled")]
        all_categories = None
        if _is_uncategorized_match(category_key):
            try:
                all_categories = skillhub.fetch_categories()
            except Exception:
                all_categories = []
        merged = _filter_skills_by_category(merged, category_key, all_categories)
        merged = _filter_skills_by_q(merged, q)
        if sort == "mtime":
            merged = enrich_skills_mtime(merged, skills_dir_for_profile(profile_key))
        merged = sort_skill_items(merged, sort=sort, order=order)
        merged = normalize_skill_list_items(merged)
        total = len(merged)
        if all_records:
            page_items = merged
            page_num = 1
            page_limit = total
        else:
            offset = (page_num - 1) * page_limit
            page_items = merged[offset : offset + page_limit]
        return _envelope(
            scope=scope_key,
            category=category_key,
            skills=page_items,
            total=total,
            page=page_num,
            page_size=page_limit,
            stats=stats,
        )

    if scope_key in ("hub", "installed", "not_installed"):
        if all_records:
            skills, total = skillhub.list_hub_catalog_filtered_from(
                ctx,
                category=category_key,
                scope=scope_key,
                q=q,
                sort=sort,
                order=order,
            )
            return _envelope(
                scope=scope_key,
                category=category_key,
                skills=skills,
                total=total,
                page=1,
                page_size=total,
                stats=stats,
            )
        skills, total = skillhub.list_hub_catalog_paged_from(
            ctx,
            category=category_key,
            scope=scope_key,
            q=q,
            page=page_num,
            page_size=page_limit,
            sort=sort,
            order=order,
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

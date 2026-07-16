"""Updated listing tests for sort parameters."""

from unittest.mock import patch

from integration.skills import listing, skillhub


_STATS = {"hub": 2, "installed": 1, "not_installed": 1, "custom": 0}


def _fake_ctx(*, hub_names: set[str] | None = None) -> skillhub._HubCatalogContext:
    names = hub_names if hub_names is not None else set()
    return skillhub._HubCatalogContext(
        raw_skills=[],
        hub_names=names,
        installed_index={},
        annotated_all=[],
        locked_names=set(),
    )


def test_list_skillhub_skills_hub_scope_envelope():
    with patch("integration.skills.listing.skillhub.build_hub_catalog_context", return_value=_fake_ctx()):
        with patch("integration.skills.listing.skillhub.compute_scope_stats_from", return_value=_STATS):
            with patch("integration.skills.listing.local_skills.scan_custom_skills_global", return_value=[]):
                with patch("integration.skills.listing.skillhub.list_hub_catalog_paged_from") as paged:
                    paged.return_value = (
                        [{"name": "a", "installed": True, "hub_installed": True, "custom": False}],
                        1,
                    )
                    result = listing.list_skillhub_skills(
                        scope="hub",
                        category="data-analysis",
                        q="data",
                        page=1,
                        page_size=9,
                        sort="mtime",
                        order="desc",
                    )
                    paged.assert_called_once()
                    assert paged.call_args.kwargs == {
                        "category": "data-analysis",
                        "scope": "hub",
                        "q": "data",
                        "page": 1,
                        "page_size": 9,
                        "sort": "mtime",
                        "order": "desc",
                    }
                    assert result["scope"] == "hub"
                    assert result["category"] == "data-analysis"
                    assert result["stats"] == _STATS
                    assert result["skills"][0]["installed"] is True


def test_list_skillhub_skills_hub_all_category():
    with patch("integration.skills.listing.skillhub.build_hub_catalog_context", return_value=_fake_ctx()):
        with patch("integration.skills.listing.skillhub.compute_scope_stats_from", return_value=_STATS):
            with patch("integration.skills.listing.local_skills.scan_custom_skills_global", return_value=[]):
                with patch("integration.skills.listing.skillhub.list_hub_catalog_paged_from") as paged:
                    paged.return_value = ([], 0)
                    listing.list_skillhub_skills(category="")
                    assert paged.call_args.kwargs["category"] == ""


def test_list_skillhub_skills_installed_scope():
    with patch("integration.skills.listing.skillhub.build_hub_catalog_context", return_value=_fake_ctx()):
        with patch("integration.skills.listing.skillhub.compute_scope_stats_from", return_value=_STATS):
            with patch("integration.skills.listing.local_skills.scan_custom_skills_global", return_value=[]):
                with patch("integration.skills.listing.skillhub.list_hub_catalog_paged_from") as paged:
                    paged.return_value = ([{"name": "a", "installed": True}], 1)
                    result = listing.list_skillhub_skills(
                        scope="installed",
                        category="tools",
                        sort="name",
                        order="asc",
                    )
                    paged.assert_called_once()
                    assert paged.call_args.kwargs["scope"] == "installed"
                    assert result["scope"] == "installed"
                    assert result["total"] == 1
                    assert result["stats"] == _STATS


def test_list_skillhub_skills_custom_scope():
    ctx = _fake_ctx(hub_names={"hub-skill"})
    scanned = [{"name": "local-only", "custom": True}]
    with patch("integration.skills.listing.skillhub.build_hub_catalog_context", return_value=ctx):
        with patch("integration.skills.listing.skillhub.compute_scope_stats_from", return_value=_STATS):
            with patch(
                "integration.skills.listing.local_skills.scan_custom_skills_global",
                return_value=scanned,
            ) as scan:
                with patch("integration.skills.listing.local_skills.list_custom_skills") as custom:
                    custom.return_value = {
                        "scope": "custom",
                        "category": "tools",
                        "skills": [{"name": "local-only", "custom": True}],
                        "total": 1,
                        "page": 1,
                        "page_size": 20,
                        "skillhub_enabled": True,
                    }
                    result = listing.list_skillhub_skills(
                        scope="custom",
                        category="tools",
                        sort="mtime",
                        order="desc",
                    )
                    scan.assert_called_once_with({"hub-skill"}, profile="default")
                    custom.assert_called_once_with(
                        category="tools",
                        profile="default",
                        q=None,
                        hub_names={"hub-skill"},
                        page=1,
                        page_size=20,
                        sort="mtime",
                        order="desc",
                        all_records=False,
                        pre_scanned=scanned,
                    )
                    assert result["scope"] == "custom"
                    assert result["stats"] == _STATS


def test_list_skillhub_skills_defaults_invalid_scope_to_hub():
    with patch("integration.skills.listing.skillhub.build_hub_catalog_context", return_value=_fake_ctx()):
        with patch("integration.skills.listing.skillhub.compute_scope_stats_from", return_value=_STATS):
            with patch("integration.skills.listing.local_skills.scan_custom_skills_global", return_value=[]):
                with patch("integration.skills.listing.skillhub.list_hub_catalog_paged_from") as paged:
                    paged.return_value = ([], 0)
                    listing.list_skillhub_skills(
                        scope="store",
                        category="tools",
                    )
                    paged.assert_called_once()
                    assert paged.call_args.kwargs["scope"] == "hub"


def test_list_skillhub_skills_hub_all_records():
    with patch("integration.skills.listing.skillhub.build_hub_catalog_context", return_value=_fake_ctx()):
        with patch("integration.skills.listing.skillhub.compute_scope_stats_from", return_value=_STATS):
            with patch("integration.skills.listing.local_skills.scan_custom_skills_global", return_value=[]):
                with patch("integration.skills.listing.skillhub.list_hub_catalog_filtered_from") as filtered:
                    filtered.return_value = (
                        [
                            {"name": "a"},
                            {"name": "b"},
                        ],
                        2,
                    )
                    result = listing.list_skillhub_skills(
                        scope="hub",
                        category="tools",
                        all_records=True,
                    )
                    filtered.assert_called_once()
                    assert filtered.call_args.kwargs == {
                        "category": "tools",
                        "scope": "hub",
                        "q": None,
                        "sort": "name",
                        "order": "asc",
                    }
                    assert len(result["skills"]) == 2
                    assert result["total"] == 2
                    assert result["page"] == 1
                    assert result["page_size"] == 2


def test_list_skillhub_skills_custom_all_records():
    with patch("integration.skills.listing.skillhub.build_hub_catalog_context", return_value=_fake_ctx()):
        with patch("integration.skills.listing.skillhub.compute_scope_stats_from", return_value=_STATS):
            with patch("integration.skills.listing.local_skills.scan_custom_skills_global", return_value=[]):
                with patch("integration.skills.listing.local_skills.list_custom_skills") as custom:
                    custom.return_value = {
                        "scope": "custom",
                        "category": "",
                        "skills": [{"name": "x"}, {"name": "y"}],
                        "total": 2,
                        "page": 1,
                        "page_size": 2,
                        "skillhub_enabled": True,
                    }
                    result = listing.list_skillhub_skills(scope="custom", all_records=True)
                    custom.assert_called_once_with(
                        category="",
                        profile="default",
                        q=None,
                        hub_names=set(),
                        page=1,
                        page_size=20,
                        sort="name",
                        order="asc",
                        all_records=True,
                        pre_scanned=[],
                    )
                    assert result["total"] == 2
                    assert result["page_size"] == 2


def _ctx_with_annotated(annotated: list[dict]) -> skillhub._HubCatalogContext:
    return skillhub._HubCatalogContext(
        raw_skills=annotated,
        hub_names={str(s.get("name") or "") for s in annotated},
        installed_index={},
        annotated_all=annotated,
        locked_names=set(),
    )


def test_local_all_merges_installed_hub_and_custom():
    annotated = [
        {"name": "hub-a", "installed": True, "hub_installed": True, "custom": False, "category": "tools"},
        {"name": "hub-b", "installed": True, "hub_installed": True, "custom": False, "category": "tools"},
        {"name": "hub-c", "installed": False, "hub_installed": False, "custom": False, "category": "tools"},
    ]
    custom = [
        {"name": "custom-1", "installed": True, "hub_installed": False, "custom": True, "category": "tools"},
    ]
    ctx = _ctx_with_annotated(annotated)
    with patch("integration.skills.listing.skillhub.build_hub_catalog_context", return_value=ctx):
        with patch("integration.skills.listing.skillhub.compute_scope_stats_from", return_value=_STATS):
            with patch("integration.skills.listing.local_skills.scan_custom_skills_global", return_value=custom):
                result = listing.list_skillhub_skills(scope="local_all", sort="name", order="asc")
    names = [s["name"] for s in result["skills"]]
    assert names == ["custom-1", "hub-a", "hub-b"]
    assert result["total"] == 3
    assert result["scope"] == "local_all"
    assert result["stats"] == _STATS


def test_local_all_custom_wins_on_name_conflict():
    annotated = [
        {"name": "shared", "installed": True, "hub_installed": True, "custom": False, "category": ""},
        {"name": "hub-only", "installed": True, "hub_installed": True, "custom": False, "category": ""},
    ]
    custom = [
        {"name": "shared", "installed": True, "hub_installed": False, "custom": True, "category": ""},
    ]
    ctx = _ctx_with_annotated(annotated)
    with patch("integration.skills.listing.skillhub.build_hub_catalog_context", return_value=ctx):
        with patch("integration.skills.listing.skillhub.compute_scope_stats_from", return_value=_STATS):
            with patch("integration.skills.listing.local_skills.scan_custom_skills_global", return_value=custom):
                result = listing.list_skillhub_skills(scope="local_all")
    names = [s["name"] for s in result["skills"]]
    assert names == ["hub-only", "shared"]
    shared_item = next(s for s in result["skills"] if s["name"] == "shared")
    assert shared_item["custom"] is True
    assert shared_item["hub_installed"] is False
    assert result["total"] == 2


def test_local_all_excludes_disabled_skills():
    annotated = [
        {"name": "hub-enabled", "installed": True, "hub_installed": True, "custom": False, "category": "", "disabled": False},
        {"name": "hub-disabled", "installed": True, "hub_installed": True, "custom": False, "category": "", "disabled": True},
    ]
    custom = [
        {"name": "custom-enabled", "installed": True, "hub_installed": False, "custom": True, "category": "", "disabled": False},
        {"name": "custom-disabled", "installed": True, "hub_installed": False, "custom": True, "category": "", "disabled": True},
    ]
    ctx = _ctx_with_annotated(annotated)
    with patch("integration.skills.listing.skillhub.build_hub_catalog_context", return_value=ctx):
        with patch("integration.skills.listing.skillhub.compute_scope_stats_from", return_value=_STATS):
            with patch("integration.skills.listing.local_skills.scan_custom_skills_global", return_value=custom):
                result = listing.list_skillhub_skills(scope="local_all", all_records=True)
    assert [skill["name"] for skill in result["skills"]] == ["custom-enabled", "hub-enabled"]
    assert result["total"] == 2
    assert result["page_size"] == 2


def test_local_all_category_filter():
    annotated = [
        {"name": "hub-tools", "installed": True, "hub_installed": True, "custom": False, "category": "tools"},
        {"name": "hub-data", "installed": True, "hub_installed": True, "custom": False, "category": "data"},
    ]
    custom = [
        {"name": "custom-tools", "installed": True, "hub_installed": False, "custom": True, "category": "tools"},
        {"name": "custom-data", "installed": True, "hub_installed": False, "custom": True, "category": "data"},
    ]
    ctx = _ctx_with_annotated(annotated)
    with patch("integration.skills.listing.skillhub.build_hub_catalog_context", return_value=ctx):
        with patch("integration.skills.listing.skillhub.compute_scope_stats_from", return_value=_STATS):
            with patch("integration.skills.listing.local_skills.scan_custom_skills_global", return_value=custom):
                result = listing.list_skillhub_skills(scope="local_all", category="tools")
    names = sorted(s["name"] for s in result["skills"])
    assert names == ["custom-tools", "hub-tools"]
    assert result["total"] == 2
    assert result["category"] == "tools"


def test_local_all_q_filter():
    annotated = [
        {"name": "alpha", "installed": True, "hub_installed": True, "custom": False, "category": "", "description": "remote tool"},
        {"name": "beta", "installed": True, "hub_installed": True, "custom": False, "category": "", "description": "other"},
    ]
    custom = [
        {"name": "gamma", "installed": True, "hub_installed": False, "custom": True, "category": "", "description": "local tool"},
    ]
    ctx = _ctx_with_annotated(annotated)
    with patch("integration.skills.listing.skillhub.build_hub_catalog_context", return_value=ctx):
        with patch("integration.skills.listing.skillhub.compute_scope_stats_from", return_value=_STATS):
            with patch("integration.skills.listing.local_skills.scan_custom_skills_global", return_value=custom):
                result = listing.list_skillhub_skills(scope="local_all", q="tool")
    names = sorted(s["name"] for s in result["skills"])
    assert names == ["alpha", "gamma"]
    assert result["total"] == 2


def test_local_all_pagination_and_all_records():
    annotated = [
        {"name": f"hub-{i}", "installed": True, "hub_installed": True, "custom": False, "category": ""}
        for i in range(5)
    ]
    custom = [
        {"name": f"custom-{i}", "installed": True, "hub_installed": False, "custom": True, "category": ""}
        for i in range(3)
    ]
    ctx = _ctx_with_annotated(annotated)
    with patch("integration.skills.listing.skillhub.build_hub_catalog_context", return_value=ctx):
        with patch("integration.skills.listing.skillhub.compute_scope_stats_from", return_value=_STATS):
            with patch("integration.skills.listing.local_skills.scan_custom_skills_global", return_value=custom):
                paged = listing.list_skillhub_skills(
                    scope="local_all", page=1, page_size=4, sort="name", order="asc"
                )
    assert paged["total"] == 8
    assert len(paged["skills"]) == 4
    assert paged["page"] == 1
    assert paged["page_size"] == 4
    with patch("integration.skills.listing.skillhub.build_hub_catalog_context", return_value=ctx):
        with patch("integration.skills.listing.skillhub.compute_scope_stats_from", return_value=_STATS):
            with patch("integration.skills.listing.local_skills.scan_custom_skills_global", return_value=custom):
                full = listing.list_skillhub_skills(
                    scope="local_all", all_records=True, sort="name", order="asc"
                )
    assert full["total"] == 8
    assert len(full["skills"]) == 8
    assert full["page"] == 1
    assert full["page_size"] == 8

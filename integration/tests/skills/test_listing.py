"""Updated listing tests for sort parameters."""

from unittest.mock import patch

from integration.skills import listing


_STATS = {"hub": 2, "installed": 1, "not_installed": 1, "custom": 0}


def test_list_skillhub_skills_hub_scope_envelope():
    with patch("integration.skills.listing.skillhub.hub_all_catalog_names", return_value=set()):
        with patch("integration.skills.listing.skillhub.compute_scope_stats", return_value=_STATS):
            with patch("integration.skills.listing.skillhub.list_hub_catalog_paged") as paged:
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
                paged.assert_called_once_with(
                    category="data-analysis",
                    scope="hub",
                    q="data",
                    page=1,
                    page_size=9,
                    sort="mtime",
                    order="desc",
                )
                assert result["scope"] == "hub"
                assert result["category"] == "data-analysis"
                assert result["stats"] == _STATS
                assert result["skills"][0]["installed"] is True


def test_list_skillhub_skills_hub_all_category():
    with patch("integration.skills.listing.skillhub.hub_all_catalog_names", return_value=set()):
        with patch("integration.skills.listing.skillhub.compute_scope_stats", return_value=_STATS):
            with patch("integration.skills.listing.skillhub.list_hub_catalog_paged") as paged:
                paged.return_value = ([], 0)
                listing.list_skillhub_skills(category="")
                assert paged.call_args.kwargs["category"] == ""


def test_list_skillhub_skills_installed_scope():
    with patch("integration.skills.listing.skillhub.hub_all_catalog_names", return_value=set()):
        with patch("integration.skills.listing.skillhub.compute_scope_stats", return_value=_STATS):
            with patch("integration.skills.listing.skillhub.list_hub_catalog_paged") as paged:
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
    with patch("integration.skills.listing.skillhub.hub_all_catalog_names") as names:
        with patch("integration.skills.listing.skillhub.compute_scope_stats", return_value=_STATS):
            with patch("integration.skills.listing.local_skills.list_custom_skills") as custom:
                names.return_value = {"hub-skill"}
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
                custom.assert_called_once_with(
                    category="tools",
                    q=None,
                    hub_names={"hub-skill"},
                    page=1,
                    page_size=20,
                    sort="mtime",
                    order="desc",
                )
                assert result["scope"] == "custom"
                assert result["stats"] == _STATS


def test_list_skillhub_skills_defaults_invalid_scope_to_hub():
    with patch("integration.skills.listing.skillhub.hub_all_catalog_names", return_value=set()):
        with patch("integration.skills.listing.skillhub.compute_scope_stats", return_value=_STATS):
            with patch("integration.skills.listing.skillhub.list_hub_catalog_paged") as paged:
                paged.return_value = ([], 0)
                listing.list_skillhub_skills(
                    scope="store",
                    category="tools",
                )
                paged.assert_called_once()
                assert paged.call_args.kwargs["scope"] == "hub"

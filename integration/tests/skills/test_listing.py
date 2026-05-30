"""Listing source selection tests."""

from unittest.mock import patch

from integration.skills import listing

_STATS = {"hub": 2, "installed": 1, "not_installed": 1, "custom": 0}


def test_list_skillhub_skills_hub_scope_envelope():
    with patch("integration.skills.listing.skillhub.hub_all_catalog_names", return_value=set()):
        with patch("integration.skills.listing.skillhub.compute_scope_stats", return_value=_STATS):
            with patch("integration.skills.listing.skillhub.fetch_catalog") as fetch:
                with patch("integration.skills.listing.skillhub.annotate_installed") as annotate:
                    fetch.return_value = {
                        "skills": [{"name": "a"}],
                        "total": 1,
                        "page": 1,
                        "page_size": 20,
                    }
                    annotate.return_value = [
                        {"name": "a", "installed": True, "hub_installed": True, "custom": False}
                    ]
                    result = listing.list_skillhub_skills(
                        scope="hub",
                        category="data-analysis",
                        q="data",
                        page=1,
                        page_size=9,
                    )
                    fetch.assert_called_once_with(
                        q="data",
                        category="data-analysis",
                        page=1,
                        page_size=9,
                    )
                    assert result["scope"] == "hub"
                    assert result["category"] == "data-analysis"
                    assert result["stats"] == _STATS
                    assert result["skills"][0]["installed"] is True


def test_list_skillhub_skills_hub_all_category():
    with patch("integration.skills.listing.skillhub.hub_all_catalog_names", return_value=set()):
        with patch("integration.skills.listing.skillhub.compute_scope_stats", return_value=_STATS):
            with patch("integration.skills.listing.skillhub.fetch_catalog") as fetch:
                with patch("integration.skills.listing.skillhub.annotate_installed", return_value=[]):
                    fetch.return_value = {"skills": [], "total": 0}
                    listing.list_skillhub_skills(category="")
                    assert fetch.call_args.kwargs["category"] is None


def test_list_skillhub_skills_installed_scope():
    with patch("integration.skills.listing.skillhub.hub_all_catalog_names", return_value=set()):
        with patch("integration.skills.listing.skillhub.compute_scope_stats", return_value=_STATS):
            with patch("integration.skills.listing.skillhub.list_hub_skills_filtered") as filtered:
                filtered.return_value = ([{"name": "a", "installed": True}], 1)
                result = listing.list_skillhub_skills(
                    scope="installed",
                    category="tools",
                )
                filtered.assert_called_once()
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
                )
                custom.assert_called_once_with(
                    category="tools",
                    q=None,
                    hub_names={"hub-skill"},
                    page=1,
                    page_size=20,
                )
                assert result["scope"] == "custom"
                assert result["stats"] == _STATS


def test_list_skillhub_skills_defaults_invalid_scope_to_hub():
    with patch("integration.skills.listing.skillhub.hub_all_catalog_names", return_value=set()):
        with patch("integration.skills.listing.skillhub.compute_scope_stats", return_value=_STATS):
            with patch("integration.skills.listing.skillhub.fetch_catalog") as fetch:
                with patch("integration.skills.listing.skillhub.annotate_installed", return_value=[]):
                    fetch.return_value = {"skills": [], "total": 0}
                    listing.list_skillhub_skills(
                        scope="store",
                        category="tools",
                    )
                    fetch.assert_called_once()

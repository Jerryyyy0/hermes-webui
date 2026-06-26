"""Listing request deduplication (single hub fetch / single custom scan per call)."""

from unittest.mock import patch

from integration.skills import listing, skillhub


def _fake_ctx() -> skillhub._HubCatalogContext:
    return skillhub._HubCatalogContext(
        raw_skills=[],
        hub_names={"hub-a"},
        installed_index={},
        annotated_all=[],
        locked_names=set(),
    )


def test_list_skillhub_skills_single_hub_fetch_installed():
    with patch("integration.skills.listing.skillhub.build_hub_catalog_context") as build_ctx:
        with patch("integration.skills.listing.local_skills.scan_custom_skills_global", return_value=[]):
            with patch(
                "integration.skills.listing.skillhub.list_hub_catalog_filtered_from",
                return_value=([], 0),
            ):
                build_ctx.return_value = _fake_ctx()
                listing.list_skillhub_skills(scope="installed", all_records=True)
                build_ctx.assert_called_once()


def test_list_skillhub_skills_single_custom_scan():
    scanned = [{"name": "local", "category": "tools", "custom": True}]
    with patch("integration.skills.listing.skillhub.build_hub_catalog_context", return_value=_fake_ctx()):
        with patch(
            "integration.skills.listing.local_skills.scan_custom_skills_global",
            return_value=scanned,
        ) as scan:
            with patch("integration.skills.listing.local_skills.list_custom_skills") as list_custom:
                list_custom.return_value = {
                    "scope": "custom",
                    "category": "",
                    "skills": scanned,
                    "total": 1,
                    "page": 1,
                    "page_size": 1,
                    "skillhub_enabled": True,
                }
                listing.list_skillhub_skills(scope="custom", all_records=True)
                scan.assert_called_once()
                assert list_custom.call_args.kwargs["pre_scanned"] is scanned


def test_listing_end_to_end_single_fetch_all_hub_skills():
    with patch("integration.skills.skillhub.fetch_all_hub_skills") as fetch_all:
        with patch("integration.skills.skillhub._hub_installed_index", return_value={}):
            with patch(
                "integration.skills.no_self_improve.get_no_self_improve_names",
                return_value=set(),
            ):
                with patch(
                    "integration.skills.local_skills.scan_custom_skills_global",
                    return_value=[],
                ):
                    fetch_all.return_value = [
                        {"name": "a", "installed": False, "category": "tools"},
                        {"name": "b", "installed": False, "category": "tools"},
                    ]
                    listing.list_skillhub_skills(scope="installed", all_records=True)
                    fetch_all.assert_called_once_with(category=None)

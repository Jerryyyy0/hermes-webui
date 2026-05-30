"""SkillHub upstream client tests (mocked httpx)."""

from unittest.mock import MagicMock, patch

import pytest

from integration.skills import skillhub


@pytest.fixture
def hub_url():
    with patch("integration.skills.skillhub.skillhub_url", return_value="http://hub.test"):
        yield


def test_fetch_catalog_no_profile_param(hub_url):
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "skills": [{"name": "a", "category": "tools"}],
        "total": 1,
        "page": 1,
        "page_size": 9,
    }
    mock_resp.raise_for_status = MagicMock()
    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.get.return_value = mock_resp

    with patch("integration.skills.skillhub._client", return_value=mock_client):
        result = skillhub.fetch_catalog(q="data", page=1, page_size=9)

    call_kwargs = mock_client.get.call_args
    assert call_kwargs[0][0] == "http://hub.test/api/skills"
    params = call_kwargs[1]["params"]
    assert "profile" not in params
    assert params["q"] == "data"
    assert params["page"] == 1
    assert result["total"] == 1
    assert result["page"] == 1


def test_fetch_categories(hub_url):
    mock_resp = MagicMock()
    mock_resp.json.return_value = ["audit", "data-analysis"]
    mock_resp.raise_for_status = MagicMock()
    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.get.return_value = mock_resp

    with patch("integration.skills.skillhub._client", return_value=mock_client):
        cats = skillhub.fetch_categories()

    assert cats == ["audit", "data-analysis"]
    assert mock_client.get.call_args[0][0] == "http://hub.test/api/skills/categories"


def test_compute_scope_stats(hub_url):
    with patch("integration.skills.skillhub.fetch_all_hub_skills") as fetch_all:
        with patch("integration.skills.skillhub.annotate_installed") as annotate:
            with patch("integration.skills.local_skills.count_custom_skills", return_value=2):
                fetch_all.return_value = [{"name": "a"}, {"name": "b"}]
                annotate.return_value = [
                    {"name": "a", "installed": True},
                    {"name": "b", "installed": False},
                ]
                stats = skillhub.compute_scope_stats(set())
                assert stats == {
                    "hub": 2,
                    "installed": 1,
                    "not_installed": 1,
                    "custom": 2,
                }


def test_compute_scope_stats_global_not_category_scoped(hub_url):
    with patch("integration.skills.skillhub.fetch_all_hub_skills") as fetch_all:
        with patch("integration.skills.skillhub.annotate_installed") as annotate:
            with patch("integration.skills.local_skills.count_custom_skills", return_value=0) as count_custom:
                fetch_all.return_value = [{"name": "a"}]
                annotate.side_effect = lambda skills: skills
                skillhub.compute_scope_stats(set())
                fetch_all.assert_called_once_with(category=None)
                count_custom.assert_called_once_with("", set())


def test_list_hub_skills_filtered_installed(hub_url):
    with patch("integration.skills.skillhub.fetch_all_hub_skills") as fetch_all:
        with patch("integration.skills.skillhub.annotate_installed") as annotate:
            fetch_all.return_value = [{"name": "a"}, {"name": "b"}]
            annotate.return_value = [
                {"name": "a", "installed": True, "custom": False},
                {"name": "b", "installed": False, "custom": False},
            ]
            skills, total = skillhub.list_hub_skills_filtered(
                "",
                "installed",
                None,
                1,
                20,
            )
            assert total == 1
            assert skills[0]["name"] == "a"


def test_hub_all_catalog_names_paginates(hub_url):
    page1 = MagicMock()
    page1.json.return_value = {
        "skills": [{"name": "a"}, {"name": "b"}],
        "total": 3,
        "page": 1,
        "page_size": 100,
    }
    page1.raise_for_status = MagicMock()
    page2 = MagicMock()
    page2.json.return_value = {
        "skills": [{"name": "c"}],
        "total": 3,
        "page": 2,
        "page_size": 100,
    }
    page2.raise_for_status = MagicMock()
    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.get.side_effect = [page1, page2]

    with patch("integration.skills.skillhub._client", return_value=mock_client):
        names = skillhub.hub_all_catalog_names()

    assert names == {"a", "b", "c"}
    assert mock_client.get.call_count == 2
    assert "category" not in mock_client.get.call_args_list[0][1]["params"]


def test_annotate_installed_sets_hub_fields(hub_url, tmp_path):
    skills_dir = tmp_path / "skills"
    installed = skills_dir / "data-analysis"
    installed.mkdir(parents=True)
    (installed / "SKILL.md").write_text("# skill", encoding="utf-8")
    (installed / ".hub_installed").write_text("1", encoding="utf-8")

    with patch("integration.skills.skillhub.shared_skills_dir", return_value=skills_dir):
        result = skillhub.annotate_installed(
            [{"name": "data-analysis"}, {"name": "other"}],
        )

    assert result[0]["installed"] is True
    assert result[0]["hub_installed"] is True
    assert result[0]["custom"] is False
    assert result[0]["dir_name"] == "data-analysis"
    assert result[0]["disabled"] is False
    assert result[1]["installed"] is False
    assert result[1]["hub_installed"] is False
    assert result[1]["custom"] is False
    assert result[1]["dir_name"] == ""
    assert result[1]["disabled"] is False


def test_fetch_doc_uses_doc_path(hub_url):
    mock_resp = MagicMock()
    mock_resp.headers = {"content-type": "application/json"}
    mock_resp.json.return_value = {"name": "x", "content": "# doc"}
    mock_resp.raise_for_status = MagicMock()
    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.get.return_value = mock_resp

    with patch("integration.skills.skillhub._client", return_value=mock_client):
        doc = skillhub.fetch_doc("data-analysis")

    assert doc["content"] == "# doc"
    assert "/api/skills/data-analysis/doc" in mock_client.get.call_args[0][0]

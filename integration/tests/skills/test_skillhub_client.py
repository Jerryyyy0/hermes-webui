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
    ctx = skillhub._HubCatalogContext(
        raw_skills=[],
        hub_names=set(),
        installed_index={},
        annotated_all=[
            {"name": "a", "installed": True},
            {"name": "b", "installed": False},
        ],
        locked_names=set(),
    )
    with patch("integration.skills.skillhub.build_hub_catalog_context", return_value=ctx):
        with patch("integration.skills.local_skills.count_custom_skills", return_value=2):
            stats = skillhub.compute_scope_stats(set())
            assert stats == {
                "hub": 2,
                "installed": 1,
                "not_installed": 1,
                "custom": 2,
            }


def test_compute_scope_stats_global_not_category_scoped(hub_url):
    ctx = skillhub._HubCatalogContext(
        raw_skills=[{"name": "a"}],
        hub_names={"a"},
        installed_index={},
        annotated_all=[{"name": "a", "installed": False}],
        locked_names=set(),
    )
    with patch("integration.skills.skillhub.build_hub_catalog_context", return_value=ctx):
        with patch("integration.skills.local_skills.count_custom_skills", return_value=0) as count_custom:
            skillhub.compute_scope_stats(set())
            count_custom.assert_called_once_with("", set())


def test_list_hub_skills_filtered_installed(hub_url):
    ctx = skillhub._HubCatalogContext(
        raw_skills=[],
        hub_names=set(),
        installed_index={},
        annotated_all=[
            {"name": "a", "installed": True, "custom": False},
            {"name": "b", "installed": False, "custom": False},
        ],
        locked_names=set(),
    )
    with patch("integration.skills.skillhub.build_hub_catalog_context", return_value=ctx):
        skills, total = skillhub.list_hub_skills_filtered(
            "",
            "installed",
            None,
            1,
            20,
        )
        assert total == 1
        assert skills[0]["name"] == "a"


def test_list_hub_catalog_paged_sort_and_pagination(hub_url):
    annotated = [
        {"name": "c", "mtime": 1.0, "installed": False},
        {"name": "a", "mtime": 3.0, "installed": False},
        {"name": "b", "mtime": 2.0, "installed": False},
    ]
    ctx = skillhub._HubCatalogContext(
        raw_skills=list(annotated),
        hub_names=skillhub._hub_names_from_skills(annotated),
        installed_index={},
        annotated_all=[dict(skill) for skill in annotated],
        locked_names=set(),
    )
    with patch("integration.skills.skillhub.build_hub_catalog_context", return_value=ctx):
        with patch("integration.skills.skillhub.enrich_skills_mtime") as enrich:
            enrich.side_effect = lambda skills, _dir: skills

            page1, total = skillhub.list_hub_catalog_paged(
                "",
                "hub",
                None,
                1,
                2,
                sort="mtime",
                order="desc",
            )
            page2, _ = skillhub.list_hub_catalog_paged(
                "",
                "hub",
                None,
                2,
                2,
                sort="mtime",
                order="desc",
            )

            assert total == 3
            assert [s["name"] for s in page1] == ["a", "b"]
            assert [s["name"] for s in page2] == ["c"]

            full, full_total = skillhub.list_hub_catalog_filtered(
                "",
                "hub",
                None,
                sort="mtime",
                order="desc",
            )
            assert full_total == 3
            assert [s["name"] for s in full] == ["a", "b", "c"]


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


def test_build_hub_catalog_context_single_fetch(hub_url):
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
        with patch("integration.skills.skillhub._hub_installed_index", return_value={}):
            with patch(
                "integration.skills.no_self_improve.get_no_self_improve_names",
                return_value=set(),
            ):
                ctx = skillhub.build_hub_catalog_context()

    assert ctx.hub_names == {"a", "b", "c"}
    assert mock_client.get.call_count == 2


def test_annotate_installed_reads_config_once(hub_url, tmp_path):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    skills = [{"name": f"skill-{index}"} for index in range(500)]

    with patch("integration.skills.skillhub.shared_skills_dir", return_value=skills_dir):
        with patch(
            "integration.skills.no_self_improve.get_no_self_improve_names",
        ) as get_names:
            get_names.return_value = {"locked-one"}
            skillhub.annotate_installed(skills)

    get_names.assert_called_once()


def test_annotate_installed_sets_hub_fields(hub_url, tmp_path):
    skills_dir = tmp_path / "skills"
    installed = skills_dir / "data-analysis"
    installed.mkdir(parents=True)
    (installed / "SKILL.md").write_text("# skill", encoding="utf-8")
    (installed / ".hub_installed").write_text("1", encoding="utf-8")

    with patch("api.profiles.list_profiles_api", return_value=[{"name": "default"}]):
        with patch("integration.skills.skillhub.skills_dir_for_profile", return_value=skills_dir):
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


def test_re_extract_skill_meta_success(hub_url):
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "name": "contract_audit",
        "skill_name": "合同审核助手",
        "display_description": "对上传的合同文本做结构化风险审核…",
        "detail_json": {"taskGoal": "识别合同中的风险条款"},
        "updated_fields": ["skill_name", "display_description", "detail_json"],
        "rows_updated": 1,
    }
    mock_resp.raise_for_status = MagicMock()
    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.post.return_value = mock_resp

    with patch("integration.skills.skillhub._client", return_value=mock_client):
        result = skillhub.re_extract_skill_meta("contract_audit")

    assert result["name"] == "contract_audit"
    assert result["skill_name"] == "合同审核助手"
    assert result["rows_updated"] == 1
    assert "skill_name" in result["updated_fields"]
    mock_client.post.assert_called_once()
    assert "/api/admin/skills/contract_audit/re-extract" in mock_client.post.call_args[0][0]


def test_re_extract_skill_meta_encodes_name(hub_url):
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"name": "my skill", "updated_fields": [], "rows_updated": 0}
    mock_resp.raise_for_status = MagicMock()
    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.post.return_value = mock_resp

    with patch("integration.skills.skillhub._client", return_value=mock_client):
        skillhub.re_extract_skill_meta("my skill")

    url = mock_client.post.call_args[0][0]
    assert "/api/admin/skills/my%20skill/re-extract" in url


def test_re_extract_skill_meta_404_raises(hub_url):
    import httpx

    mock_resp = MagicMock()
    mock_resp.status_code = 404
    mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        "Not Found", request=MagicMock(), response=mock_resp
    )
    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.post.return_value = mock_resp

    with patch("integration.skills.skillhub._client", return_value=mock_client):
        with pytest.raises(httpx.HTTPStatusError):
            skillhub.re_extract_skill_meta("nonexistent-skill")


def test_re_extract_skill_meta_502_raises(hub_url):
    import httpx

    mock_resp = MagicMock()
    mock_resp.status_code = 502
    mock_resp.json.return_value = {"detail": "大模型不可用，未落库，可重试"}
    mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        "Bad Gateway", request=MagicMock(), response=mock_resp
    )
    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.post.return_value = mock_resp

    with patch("integration.skills.skillhub._client", return_value=mock_client):
        with pytest.raises(httpx.HTTPStatusError):
            skillhub.re_extract_skill_meta("some-skill")

"""SkillHub handler routing tests."""

from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

from integration.skills.handlers import try_handle_get, try_handle_post, try_handle_post_early


def test_skills_path_not_intercepted_when_skillhub_enabled():
    parsed = urlparse("/api/skills")
    handler = MagicMock()
    with patch("integration.skills.handlers.skillhub_enabled", return_value=True):
        assert try_handle_get(handler, parsed) is False
        assert try_handle_post(handler, parsed, {}) is False


def test_skillhub_skills_route():
    parsed = urlparse("/api/skillhub/skills?category=tools&page=1")
    handler = MagicMock()
    with patch("integration.skills.handlers.skillhub_enabled", return_value=True):
        with patch("integration.skills.handlers.listing.list_skillhub_skills") as lst:
            with patch("integration.skills.handlers.j", return_value=True) as j_fn:
                lst.return_value = {
                    "skills": [],
                    "total": 0,
                    "scope": "hub",
                    "category": "tools",
                    "stats": {"hub": 0, "installed": 0, "not_installed": 0, "custom": 0},
                }
                assert try_handle_get(handler, parsed) is True
                lst.assert_called_once()
                assert lst.call_args.kwargs["category"] == "tools"
                assert "profile" not in lst.call_args.kwargs
                j_fn.assert_called_once()


def test_skillhub_skills_all_category():
    parsed = urlparse("/api/skillhub/skills")
    handler = MagicMock()
    with patch("integration.skills.handlers.skillhub_enabled", return_value=True):
        with patch("integration.skills.handlers.listing.list_skillhub_skills") as lst:
            with patch("integration.skills.handlers.j", return_value=True):
                lst.return_value = {"skills": [], "total": 0, "stats": {}}
                assert try_handle_get(handler, parsed) is True
                assert lst.call_args.kwargs["category"] == ""
                assert lst.call_args.kwargs["sort"] == "name"
                assert lst.call_args.kwargs["order"] == "asc"


def test_skillhub_skills_invalid_sort():
    parsed = urlparse("/api/skillhub/skills?sort=invalid")
    handler = MagicMock()
    with patch("integration.skills.handlers.skillhub_enabled", return_value=True):
        with patch("integration.skills.handlers.bad", return_value=True) as bad_fn:
            assert try_handle_get(handler, parsed) is True
            bad_fn.assert_called_once()
            assert bad_fn.call_args.kwargs.get("status") == 400


def test_skillhub_skills_passes_sort_order():
    parsed = urlparse("/api/skillhub/skills?sort=mtime&order=desc")
    handler = MagicMock()
    with patch("integration.skills.handlers.skillhub_enabled", return_value=True):
        with patch("integration.skills.handlers.listing.list_skillhub_skills") as lst:
            with patch("integration.skills.handlers.j", return_value=True):
                lst.return_value = {"skills": [], "total": 0, "stats": {}}
                assert try_handle_get(handler, parsed) is True
                assert lst.call_args.kwargs["sort"] == "mtime"
                assert lst.call_args.kwargs["order"] == "desc"


def test_skillhub_content_requires_name():
    parsed = urlparse("/api/skillhub/content")
    handler = MagicMock()
    with patch("integration.skills.handlers.skillhub_enabled", return_value=True):
        with patch("integration.skills.handlers.bad", return_value=True) as bad_fn:
            assert try_handle_get(handler, parsed) is True
            bad_fn.assert_called_once()
            assert bad_fn.call_args.kwargs.get("status") == 400


def test_skillhub_download_route_without_skillhub_url():
    parsed = urlparse("/api/skillhub/download?name=my-skill")
    handler = MagicMock()
    with patch("integration.skills.handlers.integration_enabled", return_value=True):
        with patch("integration.skills.handlers.skillhub_enabled", return_value=False):
            with patch("integration.skills.handlers.local_skills.prepare_skill_download") as prep:
                with patch("integration.skills.handlers.stream_zip_to_handler") as stream:
                    prep.return_value = {
                        "ok": True,
                        "zip_basename": "my-skill.zip",
                        "files": [],
                    }
                    assert try_handle_get(handler, parsed) is True
                    prep.assert_called_once_with("my-skill", "")
                    stream.assert_called_once()


def test_skillhub_download_requires_name():
    parsed = urlparse("/api/skillhub/download")
    handler = MagicMock()
    with patch("integration.skills.handlers.integration_enabled", return_value=True):
        with patch("integration.skills.handlers.bad", return_value=True) as bad_fn:
            assert try_handle_get(handler, parsed) is True
            bad_fn.assert_called_once()


def test_try_handle_post_early_upload_route():
    parsed = urlparse("/api/skillhub/upload")
    handler = MagicMock()
    with patch("integration.skills.handlers.handle_skillhub_upload", return_value=True) as upload:
        assert try_handle_post_early(handler, parsed) is True
        upload.assert_called_once_with(handler)


def test_try_handle_post_early_noop_for_other_paths():
    handler = MagicMock()
    assert try_handle_post_early(handler, urlparse("/api/skillhub/install")) is False


def test_skillhub_file_accepts_path_alias():
    parsed = urlparse("/api/skillhub/file?name=x&file=README.md")
    handler = MagicMock()
    with patch("integration.skills.handlers.skillhub_enabled", return_value=True):
        with patch("integration.skills.handlers.local_skills.has_local_skill", return_value=False):
            with patch("integration.skills.handlers.skillhub.fetch_file") as fetch:
                with patch("integration.skills.handlers.j", return_value=True):
                    fetch.return_value = {"content": "hi"}
                    assert try_handle_get(handler, parsed) is True
                    fetch.assert_called_once_with("x", "README.md")


def test_skillhub_content_auto_prefers_local():
    parsed = urlparse("/api/skillhub/content?name=hermes-agent")
    handler = MagicMock()
    with patch("integration.skills.handlers.skillhub_enabled", return_value=True):
        with patch("integration.skills.handlers.local_skills.has_local_skill", return_value=True):
            with patch("integration.skills.handlers.local_skills.get_custom_doc") as get_doc:
                with patch("integration.skills.handlers.skillhub.fetch_doc") as fetch_doc:
                    with patch("integration.skills.handlers.j", return_value=True):
                        get_doc.return_value = {"name": "hermes-agent", "content": "# local", "linked_files": {}}
                        assert try_handle_get(handler, parsed) is True
                        get_doc.assert_called_once_with("hermes-agent")
                        fetch_doc.assert_not_called()


def test_skillhub_content_auto_falls_back_to_hub():
    parsed = urlparse("/api/skillhub/content?name=remote-only")
    handler = MagicMock()
    with patch("integration.skills.handlers.skillhub_enabled", return_value=True):
        with patch("integration.skills.handlers.local_skills.has_local_skill", return_value=False):
            with patch("integration.skills.handlers.local_skills.get_custom_doc") as get_doc:
                with patch("integration.skills.handlers.skillhub.fetch_doc") as fetch_doc:
                    with patch("integration.skills.handlers.j", return_value=True):
                        fetch_doc.return_value = {"name": "remote-only", "content": "# hub", "linked_files": {}}
                        assert try_handle_get(handler, parsed) is True
                        get_doc.assert_not_called()
                        fetch_doc.assert_called_once_with("remote-only")


def test_skillhub_content_hub_scope_skips_local():
    parsed = urlparse("/api/skillhub/content?name=local-skill&scope=hub")
    handler = MagicMock()
    with patch("integration.skills.handlers.skillhub_enabled", return_value=True):
        with patch("integration.skills.handlers.local_skills.has_local_skill", return_value=True):
            with patch("integration.skills.handlers.local_skills.get_custom_doc") as get_doc:
                with patch("integration.skills.handlers.skillhub.fetch_doc") as fetch_doc:
                    with patch("integration.skills.handlers.j", return_value=True):
                        fetch_doc.return_value = {"name": "local-skill", "content": "# hub", "linked_files": {}}
                        assert try_handle_get(handler, parsed) is True
                        get_doc.assert_not_called()
                        fetch_doc.assert_called_once_with("local-skill")

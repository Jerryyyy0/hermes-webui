import json
import time
from contextlib import contextmanager
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

import pytest

from api.workspace import safe_resolve_ws, walk_workspace_files_page
from integration.workspace.handlers import try_handle_get, try_handle_post


def _patch_ws(ws_root):
    return patch(
        "integration.workspace.handlers.integration_workspace_root",
        return_value=ws_root,
    )


def _patch_resolve(ws_root):
    return patch(
        "integration.workspace.handlers.resolve_integration_rel",
        side_effect=lambda rel: safe_resolve_ws(ws_root, rel),
    )


@contextmanager
def _patch_delete_ws(ws_root):
    with patch("integration.workspace.delete.integration_workspace_root", return_value=ws_root):
        with patch(
            "integration.workspace.delete.resolve_integration_rel",
            side_effect=lambda rel: safe_resolve_ws(ws_root, rel),
        ):
            yield


def _json_payload(handler: MagicMock) -> dict:
    raw = handler.wfile.write.call_args.args[0].decode("utf-8")
    return json.loads(raw)


@pytest.fixture
def ws_root(tmp_path):
    (tmp_path / "a.txt").write_text("aaa", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("bbb", encoding="utf-8")
    (tmp_path / "sub" / "c.txt").write_text("ccc", encoding="utf-8")
    return tmp_path


def test_disabled_returns_false():
    handler = MagicMock()
    parsed = urlparse("/api/integration/workspace/files")
    with patch("integration.workspace.handlers.integration_enabled", return_value=False):
        assert try_handle_get(handler, parsed) is False


def test_files_list_page1(ws_root):
    handler = MagicMock()
    parsed = urlparse("/api/integration/workspace/files?page=1&page_size=2&sort=path&order=asc")
    with patch("integration.workspace.handlers.integration_enabled", return_value=True):
        with _patch_ws(ws_root):
            assert try_handle_get(handler, parsed) is True
    payload = _json_payload(handler)
    assert payload["page"] == 1
    assert payload["page_size"] == 2
    assert payload["sort"] == "path"
    assert payload["order"] == "asc"
    assert payload["total"] == 3
    assert payload["has_more"] is True
    assert len(payload["files"]) == 2
    paths = [f["path"] for f in payload["files"]]
    assert paths == ["a.txt", "sub/b.txt"]


def test_files_list_page2(ws_root):
    handler = MagicMock()
    parsed = urlparse("/api/integration/workspace/files?page=2&page_size=2&sort=path&order=asc")
    with patch("integration.workspace.handlers.integration_enabled", return_value=True):
        with _patch_ws(ws_root):
            assert try_handle_get(handler, parsed) is True
    payload = _json_payload(handler)
    assert len(payload["files"]) == 1
    assert payload["files"][0]["path"] == "sub/c.txt"
    assert payload["has_more"] is False


def test_files_list_default_sort_path_desc(ws_root):
    handler = MagicMock()
    parsed = urlparse("/api/integration/workspace/files?page=1&page_size=10")
    with patch("integration.workspace.handlers.integration_enabled", return_value=True):
        with _patch_ws(ws_root):
            assert try_handle_get(handler, parsed) is True
    payload = _json_payload(handler)
    assert payload["sort"] == "path"
    assert payload["order"] == "desc"
    paths = [f["path"] for f in payload["files"]]
    assert paths == ["sub/c.txt", "sub/b.txt", "a.txt"]


def test_files_list_empty_page(ws_root):
    handler = MagicMock()
    parsed = urlparse("/api/integration/workspace/files?page=99&page_size=10")
    with patch("integration.workspace.handlers.integration_enabled", return_value=True):
        with _patch_ws(ws_root):
            assert try_handle_get(handler, parsed) is True
    payload = _json_payload(handler)
    assert payload["files"] == []
    assert payload["total"] == 3
    assert payload["has_more"] is False


def test_files_invalid_page(ws_root):
    handler = MagicMock()
    parsed = urlparse("/api/integration/workspace/files?page=0")
    with patch("integration.workspace.handlers.integration_enabled", return_value=True):
        with _patch_ws(ws_root):
            assert try_handle_get(handler, parsed) is True
    handler.send_response.assert_called_with(400)


def test_files_invalid_sort(ws_root):
    handler = MagicMock()
    parsed = urlparse("/api/integration/workspace/files?sort=type")
    with patch("integration.workspace.handlers.integration_enabled", return_value=True):
        with _patch_ws(ws_root):
            assert try_handle_get(handler, parsed) is True
    handler.send_response.assert_called_with(400)


def test_files_invalid_order(ws_root):
    handler = MagicMock()
    parsed = urlparse("/api/integration/workspace/files?order=up")
    with patch("integration.workspace.handlers.integration_enabled", return_value=True):
        with _patch_ws(ws_root):
            assert try_handle_get(handler, parsed) is True
    handler.send_response.assert_called_with(400)


def test_files_invalid_type_filter(ws_root):
    handler = MagicMock()
    parsed = urlparse("/api/integration/workspace/files?type=.")
    with patch("integration.workspace.handlers.integration_enabled", return_value=True):
        with _patch_ws(ws_root):
            assert try_handle_get(handler, parsed) is True
    handler.send_response.assert_called_with(400)


def test_files_search_basename_only(ws_root):
    (ws_root / "report.md").write_text("# r", encoding="utf-8")
    (ws_root / "nested").mkdir(exist_ok=True)
    (ws_root / "nested" / "other.txt").write_text("x", encoding="utf-8")

    handler = MagicMock()
    parsed = urlparse("/api/integration/workspace/files?q=report&sort=path&order=asc")
    with patch("integration.workspace.handlers.integration_enabled", return_value=True):
        with _patch_ws(ws_root):
            assert try_handle_get(handler, parsed) is True
    payload = _json_payload(handler)
    paths = [f["path"] for f in payload["files"]]
    assert paths == ["report.md"]
    assert payload["q"] == "report"


def test_files_type_filter_by_extension(ws_root):
    (ws_root / "note.md").write_text("# n", encoding="utf-8")
    (ws_root / "script.py").write_text("print(1)", encoding="utf-8")

    handler = MagicMock()
    parsed = urlparse("/api/integration/workspace/files?type=.md&sort=path&order=asc")
    with patch("integration.workspace.handlers.integration_enabled", return_value=True):
        with _patch_ws(ws_root):
            assert try_handle_get(handler, parsed) is True
    payload = _json_payload(handler)
    paths = [f["path"] for f in payload["files"]]
    assert paths == ["note.md"]
    assert payload["type"] == ".md"
    assert all(f["ext"] == ".md" for f in payload["files"])


def test_files_type_filter_extension_without_dot(ws_root):
    (ws_root / "note.md").write_text("# n", encoding="utf-8")

    handler = MagicMock()
    parsed = urlparse("/api/integration/workspace/files?type=md&sort=path&order=asc")
    with patch("integration.workspace.handlers.integration_enabled", return_value=True):
        with _patch_ws(ws_root):
            assert try_handle_get(handler, parsed) is True
    payload = _json_payload(handler)
    assert payload["type"] == ".md"
    assert [f["path"] for f in payload["files"]] == ["note.md"]


def test_files_entry_type_fields(ws_root):
    handler = MagicMock()
    parsed = urlparse("/api/integration/workspace/files?sort=path&order=asc")
    with patch("integration.workspace.handlers.integration_enabled", return_value=True):
        with _patch_ws(ws_root):
            assert try_handle_get(handler, parsed) is True
    payload = _json_payload(handler)
    entry = next(f for f in payload["files"] if f["path"] == "a.txt")
    assert entry["ext"] == ".txt"
    assert "category" not in entry
    assert entry["mime"] == "application/octet-stream"
    assert "mtime_ns" in entry
    assert entry["ctime_ns"] is not None


def test_files_sort_by_size_desc(tmp_path):
    (tmp_path / "small.txt").write_text("a", encoding="utf-8")
    (tmp_path / "large.txt").write_text("a" * 100, encoding="utf-8")

    handler = MagicMock()
    parsed = urlparse("/api/integration/workspace/files?sort=size&order=desc")
    with patch("integration.workspace.handlers.integration_enabled", return_value=True):
        with _patch_ws(tmp_path):
            assert try_handle_get(handler, parsed) is True
    payload = _json_payload(handler)
    paths = [f["path"] for f in payload["files"]]
    assert paths[0] == "large.txt"
    assert paths[1] == "small.txt"


def test_files_sort_by_mtime_desc(tmp_path):
    old = tmp_path / "old.txt"
    new = tmp_path / "new.txt"
    old.write_text("old", encoding="utf-8")
    time.sleep(0.05)
    new.write_text("new", encoding="utf-8")

    handler = MagicMock()
    parsed = urlparse("/api/integration/workspace/files?sort=mtime&order=desc")
    with patch("integration.workspace.handlers.integration_enabled", return_value=True):
        with _patch_ws(tmp_path):
            assert try_handle_get(handler, parsed) is True
    payload = _json_payload(handler)
    paths = [f["path"] for f in payload["files"]]
    assert paths[0] == "new.txt"
    assert paths[1] == "old.txt"


def test_file_stream_invokes_serve_without_disposition(ws_root):
    handler = MagicMock()
    parsed = urlparse("/api/integration/workspace/file?path=a.txt")
    with patch("integration.workspace.handlers.integration_enabled", return_value=True):
        with _patch_resolve(ws_root):
            with patch("api.routes._serve_file_bytes", return_value=True) as serve:
                assert try_handle_get(handler, parsed) is True
                serve.assert_called_once()
                assert serve.call_args[0][3] is None


def test_file_stream_missing_path(ws_root):
    handler = MagicMock()
    parsed = urlparse("/api/integration/workspace/file")
    with patch("integration.workspace.handlers.integration_enabled", return_value=True):
        with _patch_ws(ws_root):
            assert try_handle_get(handler, parsed) is True
    handler.send_response.assert_called_with(400)


def test_file_raw_path_removed(ws_root):
    handler = MagicMock()
    parsed = urlparse("/api/integration/workspace/file/raw?path=a.txt")
    with patch("integration.workspace.handlers.integration_enabled", return_value=True):
        assert try_handle_get(handler, parsed) is False


def test_walk_unit(ws_root):
    page1 = walk_workspace_files_page(ws_root, ".", page=1, page_size=2, sort="path", order="asc")
    assert [f["path"] for f in page1["files"]] == ["a.txt", "sub/b.txt"]
    assert page1["total"] == 3
    assert page1["has_more"] is True
    page2 = walk_workspace_files_page(ws_root, ".", page=2, page_size=2, sort="path", order="asc")
    assert [f["path"] for f in page2["files"]] == ["sub/c.txt"]
    assert page2["has_more"] is False


def test_files_list_excludes_cruft(tmp_path):
    (tmp_path / "ok.txt").write_text("x", encoding="utf-8")
    (tmp_path / ".DS_Store").write_bytes(b"\x00")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "._foo").write_bytes(b"\x00")
    (tmp_path / "sub" / "real.txt").write_text("y", encoding="utf-8")

    handler = MagicMock()
    parsed = urlparse("/api/integration/workspace/files?page=1&page_size=50&sort=path&order=asc")
    with patch("integration.workspace.handlers.integration_enabled", return_value=True):
        with _patch_ws(tmp_path):
            assert try_handle_get(handler, parsed) is True
    paths = [f["path"] for f in _json_payload(handler)["files"]]
    assert paths == ["ok.txt", "sub/real.txt"]
    assert ".DS_Store" not in paths
    assert "sub/._foo" not in paths


def test_file_stream_cruft_returns_404(tmp_path):
    (tmp_path / ".DS_Store").write_bytes(b"\x00")
    handler = MagicMock()
    parsed = urlparse("/api/integration/workspace/file?path=.DS_Store")
    with patch("integration.workspace.handlers.integration_enabled", return_value=True):
        with _patch_resolve(tmp_path):
            assert try_handle_get(handler, parsed) is True
    handler.send_response.assert_called_with(404)


def test_files_annotate_profile_from_index(ws_root):
    handler = MagicMock()
    parsed = urlparse("/api/integration/workspace/files?sort=path&order=asc")
    with patch("integration.workspace.handlers.integration_enabled", return_value=True):
        with _patch_ws(ws_root):
            with patch(
                "integration.workspace.handlers.get_artifact_profile_index",
                return_value={"sub/b.txt": "ops"},
            ):
                assert try_handle_get(handler, parsed) is True
    payload = _json_payload(handler)
    by_path = {f["path"]: f for f in payload["files"]}
    assert by_path["a.txt"].get("profile") is None
    assert by_path["sub/b.txt"].get("profile") == "ops"
    assert by_path["sub/c.txt"].get("profile") is None


def test_files_profile_filter_returns_only_matching_artifacts(ws_root):
    handler = MagicMock()
    parsed = urlparse("/api/integration/workspace/files?profile=ops&sort=path&order=asc")
    with patch("integration.workspace.handlers.integration_enabled", return_value=True):
        with _patch_ws(ws_root):
            with patch(
                "integration.workspace.handlers.get_artifact_paths_for_profile",
                return_value=frozenset({"sub/b.txt"}),
            ):
                assert try_handle_get(handler, parsed) is True
    payload = _json_payload(handler)
    assert payload["profile"] == "ops"
    paths = [f["path"] for f in payload["files"]]
    assert paths == ["sub/b.txt"]
    assert all(f.get("profile") == "ops" for f in payload["files"])


def test_files_profile_filter_empty_returns_no_files(ws_root):
    handler = MagicMock()
    parsed = urlparse("/api/integration/workspace/files?profile=ops&sort=path&order=asc")
    with patch("integration.workspace.handlers.integration_enabled", return_value=True):
        with _patch_ws(ws_root):
            with patch(
                "integration.workspace.handlers.get_artifact_paths_for_profile",
                return_value=frozenset(),
            ):
                assert try_handle_get(handler, parsed) is True
    payload = _json_payload(handler)
    assert payload["profile"] == "ops"
    assert payload["files"] == []
    assert payload["total"] == 0


def test_walk_allowed_paths_filter(ws_root):
    page = walk_workspace_files_page(
        ws_root,
        ".",
        page=1,
        page_size=10,
        sort="path",
        order="asc",
        allowed_paths=frozenset({"a.txt"}),
    )
    assert [f["path"] for f in page["files"]] == ["a.txt"]
    assert page["total"] == 1


def test_files_refresh_forces_rebuild(ws_root):
    handler = MagicMock()
    parsed = urlparse("/api/integration/workspace/files?page=1&page_size=50&refresh=1&sort=path&order=asc")
    with patch("integration.workspace.handlers.integration_enabled", return_value=True):
        with _patch_ws(ws_root):
            with patch(
                "integration.workspace.handlers.get_workspace_file_entries",
                return_value=[{"path": "a.txt", "size": 3, "ext": ".txt", "mime": "text/plain"}],
            ) as get_entries:
                assert try_handle_get(handler, parsed) is True
                get_entries.assert_called_once()
                assert get_entries.call_args.kwargs.get("force_refresh") is True


def test_files_profile_filter_uses_path_collect(ws_root):
    handler = MagicMock()
    parsed = urlparse("/api/integration/workspace/files?profile=ops&sort=path&order=asc")
    with patch("integration.workspace.handlers.integration_enabled", return_value=True):
        with _patch_ws(ws_root):
            with patch(
                "integration.workspace.handlers.get_artifact_paths_for_profile",
                return_value=frozenset({"a.txt"}),
            ) as get_paths:
                with patch(
                    "integration.workspace.handlers.collect_workspace_file_entries_for_paths",
                    return_value=[{"path": "a.txt", "size": 3, "ext": ".txt", "mime": "text/plain"}],
                ) as collect:
                    assert try_handle_get(handler, parsed) is True
                    get_paths.assert_called_once_with("ops", workspace_root=ws_root)
                    collect.assert_called_once()
                    args, _ = collect.call_args
                    assert args[1] == frozenset({"a.txt"})


def test_files_no_profile_skips_path_collect(ws_root):
    handler = MagicMock()
    parsed = urlparse("/api/integration/workspace/files?sort=path&order=asc")
    with patch("integration.workspace.handlers.integration_enabled", return_value=True):
        with _patch_ws(ws_root):
            with patch(
                "integration.workspace.handlers.collect_workspace_file_entries_for_paths",
            ) as collect:
                assert try_handle_get(handler, parsed) is True
                collect.assert_not_called()


def test_post_disabled_returns_false():
    handler = MagicMock()
    parsed = urlparse("/api/integration/workspace/file/delete")
    with patch("integration.workspace.handlers.integration_enabled", return_value=False):
        assert try_handle_post(handler, parsed, {"paths": ["a.txt"]}) is False


def test_delete_missing_paths_400():
    handler = MagicMock()
    parsed = urlparse("/api/integration/workspace/file/delete")
    with patch("integration.workspace.handlers.integration_enabled", return_value=True):
        assert try_handle_post(handler, parsed, {}) is True
    assert handler.send_response.call_args.args[0] == 400


def test_delete_file_success(ws_root):
    handler = MagicMock()
    parsed = urlparse("/api/integration/workspace/file/delete")
    target = ws_root / "a.txt"
    assert target.exists()
    with patch("integration.workspace.handlers.integration_enabled", return_value=True):
        with _patch_delete_ws(ws_root):
            assert try_handle_post(handler, parsed, {"paths": ["a.txt"]}) is True
    assert handler.send_response.call_args.args[0] == 200
    payload = _json_payload(handler)
    assert payload["ok"] is True
    assert payload["deleted"] == ["a.txt"]
    assert not target.exists()


def test_delete_file_batch_partial(ws_root):
    handler = MagicMock()
    parsed = urlparse("/api/integration/workspace/file/delete")
    with patch("integration.workspace.handlers.integration_enabled", return_value=True):
        with _patch_delete_ws(ws_root):
            assert try_handle_post(
                handler,
                parsed,
                {"paths": ["a.txt", "missing.txt"]},
            ) is True
    payload = _json_payload(handler)
    assert payload["ok"] is True
    assert payload["deleted"] == ["a.txt"]
    assert payload["failed"] == [{"path": "missing.txt", "error": "not found"}]


def test_delete_all_missing_404(ws_root):
    handler = MagicMock()
    parsed = urlparse("/api/integration/workspace/file/delete")
    with patch("integration.workspace.handlers.integration_enabled", return_value=True):
        with _patch_delete_ws(ws_root):
            assert try_handle_post(handler, parsed, {"paths": ["nope.txt"]}) is True
    assert handler.send_response.call_args.args[0] == 404
    payload = _json_payload(handler)
    assert payload["ok"] is False


def test_delete_directory_fails(ws_root):
    handler = MagicMock()
    parsed = urlparse("/api/integration/workspace/file/delete")
    with patch("integration.workspace.handlers.integration_enabled", return_value=True):
        with _patch_delete_ws(ws_root):
            assert try_handle_post(handler, parsed, {"paths": ["sub"]}) is True
    payload = _json_payload(handler)
    assert payload["deleted"] == []
    assert payload["failed"][0]["error"] == "not a file"
    assert (ws_root / "sub").is_dir()


def test_delete_invalidates_index(ws_root):
    handler = MagicMock()
    parsed = urlparse("/api/integration/workspace/file/delete")
    with patch("integration.workspace.handlers.integration_enabled", return_value=True):
        with _patch_delete_ws(ws_root):
            with patch("integration.workspace.delete.invalidate_workspace_file_index") as invalidate:
                try_handle_post(handler, parsed, {"paths": ["a.txt"]})
                invalidate.assert_called_once()

import threading
from unittest.mock import patch

import pytest

from integration.workspace.file_index_cache import (
    get_workspace_file_entries,
    invalidate_workspace_file_index,
)


@pytest.fixture
def ws_root(tmp_path):
    (tmp_path / "a.txt").write_text("aaa", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("bbb", encoding="utf-8")
    return tmp_path


def test_cache_hit_avoids_second_walk(ws_root):
    with patch("integration.workspace.file_index_cache._collect_workspace_file_entries") as collect:
        collect.return_value = [{"path": "a.txt", "size": 3}]
        first = get_workspace_file_entries(ws_root, ".")
        second = get_workspace_file_entries(ws_root, ".")
        assert first == second
        assert collect.call_count == 1


def test_force_refresh_rebuilds(ws_root):
    with patch("integration.workspace.file_index_cache._collect_workspace_file_entries") as collect:
        collect.side_effect = [
            [{"path": "a.txt", "size": 3}],
            [{"path": "a.txt", "size": 3}, {"path": "sub/b.txt", "size": 3}],
        ]
        first = get_workspace_file_entries(ws_root, ".")
        second = get_workspace_file_entries(ws_root, ".", force_refresh=True)
        assert len(first) == 1
        assert len(second) == 2
        assert collect.call_count == 2


def test_invalidate_workspace_only(ws_root, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    (other / "x.txt").write_text("x", encoding="utf-8")

    with patch("integration.workspace.file_index_cache._collect_workspace_file_entries") as collect:
        collect.side_effect = lambda ws, rel: [{"path": str(ws.name)}]
        get_workspace_file_entries(ws_root, ".")
        get_workspace_file_entries(other, ".")
        assert collect.call_count == 2

        invalidate_workspace_file_index(ws_root)
        get_workspace_file_entries(ws_root, ".")
        get_workspace_file_entries(other, ".")
        assert collect.call_count == 3


def test_concurrent_rebuild_coalesced(ws_root):
    barrier = threading.Barrier(2, timeout=5)
    results: list[list[dict]] = []
    errors: list[Exception] = []

    def worker():
        try:
            barrier.wait()
            results.append(get_workspace_file_entries(ws_root, "."))
        except Exception as exc:
            errors.append(exc)

    with patch("integration.workspace.file_index_cache._collect_workspace_file_entries") as collect:
        collect.return_value = [{"path": "a.txt", "size": 3}]
        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        assert not errors
        assert len(results) == 2
        assert results[0] == results[1]
        assert collect.call_count == 1

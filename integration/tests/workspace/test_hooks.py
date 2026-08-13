from types import SimpleNamespace

from integration.workspace.file_index_cache import get_workspace_file_entries
from integration.workspace.hooks import _on_session_saved


def test_session_save_invalidates_parent_integration_workspace_index(tmp_path, monkeypatch):
    integration_root = tmp_path / "workspace"
    session_workspace = integration_root / "sessions" / "c94bb01f8413"
    session_workspace.mkdir(parents=True)
    (session_workspace / "before.txt").write_text("before", encoding="utf-8")

    monkeypatch.setattr(
        "integration.workspace._root.integration_workspace_root",
        lambda: integration_root,
    )

    # _on_session_saved imports the configuration helper locally.
    monkeypatch.setattr("integration.config.integration_enabled", lambda: True)

    initial_paths = {
        entry["path"] for entry in get_workspace_file_entries(integration_root, ".")
    }
    assert initial_paths == {"sessions/c94bb01f8413/before.txt"}

    (session_workspace / "today.docx").write_bytes(b"document")
    _on_session_saved(SimpleNamespace(workspace=str(session_workspace)))

    refreshed_paths = {
        entry["path"] for entry in get_workspace_file_entries(integration_root, ".")
    }
    assert refreshed_paths == {
        "sessions/c94bb01f8413/before.txt",
        "sessions/c94bb01f8413/today.docx",
    }


def test_session_save_keeps_external_workspace_cache_scope(tmp_path, monkeypatch):
    integration_root = tmp_path / "workspace"
    external_workspace = tmp_path / "external"
    integration_root.mkdir()
    external_workspace.mkdir()

    invalidated = []
    monkeypatch.setattr("integration.config.integration_enabled", lambda: True)
    monkeypatch.setattr(
        "integration.workspace._root.integration_workspace_root",
        lambda: integration_root,
    )
    monkeypatch.setattr(
        "integration.workspace.file_index_cache.invalidate_workspace_file_index",
        lambda workspace: invalidated.append(workspace),
    )

    _on_session_saved(SimpleNamespace(workspace=str(external_workspace)))

    assert invalidated == [external_workspace]

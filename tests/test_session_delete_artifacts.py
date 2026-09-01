from pathlib import Path
from types import SimpleNamespace


def _session(session_id: str, workspace: Path) -> SimpleNamespace:
    return SimpleNamespace(session_id=session_id, workspace=str(workspace), profile="")


def test_session_delete_artifact_setting_defaults_off_and_persists(tmp_path, monkeypatch):
    import api.config as config

    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "settings.json")

    assert config.load_settings()["session_delete_artifact"] is False
    assert config.save_settings({"session_delete_artifact": True})["session_delete_artifact"] is True
    assert config.load_settings()["session_delete_artifact"] is True


def test_delete_session_artifact_files_only_removes_regular_workspace_files(tmp_path):
    from integration.session_manifest.store import (
        delete_session_artifact_files,
        upsert_manifest_records,
    )

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifact = workspace / "nested" / "report.md"
    artifact.parent.mkdir()
    artifact.write_text("report", encoding="utf-8")
    external = tmp_path / "external.txt"
    external.write_text("external", encoding="utf-8")
    link = workspace / "external-link.txt"
    link.symlink_to(external)
    session = _session("deleteartifact01", workspace)
    db_path = tmp_path / "session_manifest.db"

    upsert_manifest_records(
        session,
        "turn:1",
        [
            {"path": "nested/report.md", "source_tool": "write_file", "preview": "file"},
            {"path": external.as_posix(), "source_tool": "write_file", "preview": "file"},
            {"path": "external-link.txt", "source_tool": "write_file", "preview": "file"},
            {"path": "missing.md", "source_tool": "write_file", "preview": "file"},
        ],
        db_path=db_path,
    )

    result = delete_session_artifact_files(session.session_id, db_path=db_path)

    assert result == {"deleted": 1, "missing": 1, "skipped": 2, "failed": 0}
    assert not artifact.exists()
    assert external.read_text(encoding="utf-8") == "external"
    assert link.is_symlink()


def test_session_delete_uses_the_saved_artifact_setting(monkeypatch, tmp_path):
    import api.background_process as background_process
    import api.config as config
    import api.models as models
    import api.routes as routes
    import api.run_journal as run_journal
    import api.terminal as terminal
    import api.turn_journal as turn_journal
    import api.upload as upload
    from integration.session_manifest import store

    sid = "deleteartifact02"
    cleanup_calls = []
    response = {}
    monkeypatch.setattr(routes, "read_body", lambda _handler: {"session_id": sid})
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "SESSION_DIR", tmp_path / "sessions")
    monkeypatch.setattr(routes, "load_settings", lambda: {"session_delete_artifact": True})
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda _sid: {})
    monkeypatch.setattr(routes, "_is_messaging_session_id", lambda _sid: False)
    monkeypatch.setattr(routes, "_worktree_retained_payload_for_session_id", lambda _sid: {})
    monkeypatch.setattr(
        routes,
        "get_session",
        lambda _sid, metadata_only=False: SimpleNamespace(profile="", session_id=sid),
    )
    monkeypatch.setattr(routes, "prune_session_from_index", lambda _sid: None)
    monkeypatch.setattr(routes, "_publish_session_list_changed", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(config, "_evict_session_agent", lambda _sid: None)
    monkeypatch.setattr(upload, "_session_attachment_dir", lambda _sid: tmp_path / "attachments" / _sid)
    monkeypatch.setattr(turn_journal, "delete_turn_journal", lambda _sid: None)
    monkeypatch.setattr(run_journal, "delete_run_journal", lambda _sid: None)
    monkeypatch.setattr(store, "delete_session_manifest_records", lambda _sid: None)
    monkeypatch.setattr(store, "delete_session_artifact_files", lambda _sid: cleanup_calls.append(_sid) or {
        "deleted": 1,
        "missing": 0,
        "skipped": 0,
        "failed": 0,
    })
    monkeypatch.setattr(models, "delete_cli_session", lambda _sid: True)
    monkeypatch.setattr(background_process, "forget_bg_task_completion_dedup", lambda _sid: None)
    monkeypatch.setattr(terminal, "close_terminal", lambda _sid: None)
    monkeypatch.setattr(routes, "j", lambda _handler, payload, **_kwargs: response.update(payload) or True)

    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/delete")) is True
    assert cleanup_calls == [sid]
    assert response == {"ok": True, "state_db_cleanup_failed": False}

"""Regression coverage for server-managed per-session workspaces."""

import json
from types import SimpleNamespace

import pytest

import api.models as models
import api.routes as routes
from api.models import SESSIONS, Session, new_session
from api.workspace import create_managed_workspace, resolve_session_workspace


@pytest.fixture(autouse=True)
def _isolate_sessions(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    SESSIONS.clear()
    yield session_dir
    SESSIONS.clear()


def _post_new(monkeypatch, body, *, remote_terminal=False, docker_terminal=None):
    captured = {}
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "read_body", lambda _handler: body)
    monkeypatch.setattr(routes, "_worktree_default_from_config", lambda _profile: False)
    monkeypatch.setattr(routes, "_terminal_remote_backend_enabled", lambda: remote_terminal)
    if docker_terminal is not None:
        monkeypatch.setattr(routes, "_terminal_docker_backend_enabled", lambda: docker_terminal)
    monkeypatch.setattr(
        routes,
        "j",
        lambda _handler, payload, status=200, extra_headers=None: captured.update(
            payload=payload, status=status
        ) or True,
    )
    monkeypatch.setattr(
        routes,
        "bad",
        lambda _handler, msg, status=400: captured.update(error=msg, status=status) or True,
    )
    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/new")) is True
    return captured


def test_session_new_without_workspace_creates_and_persists_managed_root(tmp_path, monkeypatch):
    base = tmp_path / "workspace-base"
    base.mkdir()
    monkeypatch.setattr(routes, "DEFAULT_WORKSPACE", base)

    result = _post_new(monkeypatch, {})

    assert result["status"] == 200
    session_data = result["payload"]["session"]
    sid = session_data["session_id"]
    root = base / "sessions" / sid
    assert session_data["workspace"] == str(root.resolve())
    assert root.is_dir()
    assert "workspace_mode" not in session_data

    saved = json.loads((models.SESSION_DIR / f"{sid}.json").read_text(encoding="utf-8"))
    assert saved["workspace"] == str(root.resolve())
    assert saved["workspace_mode"] == "managed"
    assert Session.load(sid).workspace_mode == "managed"


def test_session_new_with_explicit_workspace_stays_external(tmp_path, monkeypatch):
    base = tmp_path / "workspace-base"
    external = tmp_path / "existing-project"
    base.mkdir()
    external.mkdir()
    monkeypatch.setattr(routes, "DEFAULT_WORKSPACE", base)
    monkeypatch.setattr(routes, "resolve_trusted_workspace", lambda raw: external.resolve())
    monkeypatch.setattr(
        routes,
        "create_managed_workspace",
        lambda *_args, **_kwargs: pytest.fail("explicit workspace must not allocate a managed root"),
    )

    result = _post_new(monkeypatch, {"workspace": str(external)})

    assert result["status"] == 200
    sid = result["payload"]["session"]["session_id"]
    assert result["payload"]["session"]["workspace"] == str(external.resolve())
    assert not (base / "sessions" / sid).exists()
    # Ordinary zero-message external sessions retain the historic in-memory
    # lifecycle. Only managed roots need an immediate sidecar write.
    assert SESSIONS[sid].workspace_mode == "external"
    assert Session.load(sid) is None


def test_session_new_on_remote_terminal_keeps_existing_remote_workspace(tmp_path, monkeypatch):
    base = tmp_path / "workspace-base"
    remote_cwd = tmp_path / "remote-cwd-hint"
    base.mkdir()
    remote_cwd.mkdir()
    monkeypatch.setattr(routes, "DEFAULT_WORKSPACE", base)
    monkeypatch.setattr(routes, "get_last_workspace", lambda: str(remote_cwd))
    monkeypatch.setattr(routes, "resolve_trusted_workspace", lambda raw: remote_cwd.resolve())
    monkeypatch.setattr(
        routes,
        "create_managed_workspace",
        lambda *_args, **_kwargs: pytest.fail("remote terminal must not create a host managed root"),
    )

    result = _post_new(monkeypatch, {}, remote_terminal=True, docker_terminal=False)

    assert result["status"] == 200
    sid = result["payload"]["session"]["session_id"]
    assert result["payload"]["session"]["workspace"] == str(remote_cwd.resolve())
    assert SESSIONS[sid].workspace_mode == "external"


def test_session_new_on_docker_terminal_creates_managed_workspace(tmp_path, monkeypatch):
    base = tmp_path / "workspace-base"
    base.mkdir()
    monkeypatch.setattr(routes, "DEFAULT_WORKSPACE", base)
    monkeypatch.setattr(
        routes,
        "get_last_workspace",
        lambda: pytest.fail("Docker must not fall back to the remote cwd"),
    )
    monkeypatch.setattr(routes, "get_config", lambda: {"terminal": {"backend": "docker"}})

    result = _post_new(monkeypatch, {}, remote_terminal=True)

    assert result["status"] == 200
    session_data = result["payload"]["session"]
    sid = session_data["session_id"]
    root = base / "sessions" / sid
    assert session_data["workspace"] == str(root.resolve())
    assert root.is_dir()
    assert SESSIONS[sid].workspace_mode == "managed"
    assert Session.load(sid).workspace_mode == "managed"


def test_managed_session_persistence_failure_keeps_directory_but_not_memory_session(tmp_path, monkeypatch):
    base = tmp_path / "workspace-base"
    base.mkdir()
    monkeypatch.setattr(routes, "DEFAULT_WORKSPACE", base)
    monkeypatch.setattr(models.Session, "save", lambda _self: (_ for _ in ()).throw(OSError("disk full")))

    result = _post_new(monkeypatch, {})

    assert result["status"] == 500
    assert "persist managed session" in result["error"]
    assert len(list(base.iterdir())) == 1
    assert not SESSIONS


def test_managed_workspace_creation_rejects_preexisting_symlink(tmp_path):
    base = tmp_path / "workspace-base"
    outside = tmp_path / "outside"
    base.mkdir()
    outside.mkdir()
    namespace = base / "sessions"
    namespace.mkdir()
    (namespace / "session01").symlink_to(outside, target_is_directory=True)

    with pytest.raises(FileExistsError):
        create_managed_workspace("session01", base)
    assert outside.is_dir()


def test_managed_workspace_creation_rejects_symlinked_sessions_namespace(tmp_path):
    base = tmp_path / "workspace-base"
    outside = tmp_path / "outside"
    base.mkdir()
    outside.mkdir()
    (base / "sessions").symlink_to(outside, target_is_directory=True)

    with pytest.raises(FileNotFoundError):
        create_managed_workspace("session01", base)
    assert not (outside / "session01").exists()


def test_managed_session_workspace_is_immutable_for_session_update(tmp_path, monkeypatch):
    base = tmp_path / "workspace-base"
    first = base / "first"
    second = base / "second"
    first.mkdir(parents=True)
    second.mkdir()
    monkeypatch.setattr("api.workspace._BOOT_DEFAULT_WORKSPACE", base)
    session = new_session(workspace=str(first), workspace_mode="managed")
    captured = {}
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(
        routes,
        "read_body",
        lambda _handler: {"session_id": session.session_id, "workspace": str(second)},
    )
    monkeypatch.setattr(
        routes,
        "bad",
        lambda _handler, msg, status=400: captured.update(error=msg, status=status) or True,
    )

    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/update")) is True
    assert captured["status"] == 409
    assert session.workspace == str(first.resolve())


def test_managed_resolver_accepts_only_its_persisted_root(tmp_path, monkeypatch):
    base = tmp_path / "workspace-base"
    first = base / "first"
    second = base / "second"
    first.mkdir(parents=True)
    second.mkdir()
    monkeypatch.setattr("api.workspace._BOOT_DEFAULT_WORKSPACE", base)
    session = SimpleNamespace(workspace=str(first), workspace_mode="managed")

    assert resolve_session_workspace(session, str(first)) == first.resolve()
    with pytest.raises(ValueError, match="cannot be changed"):
        resolve_session_workspace(session, str(second))


def test_internal_worker_can_reuse_prevalidated_external_workspace(tmp_path):
    workspace = tmp_path / "worker-workspace"
    workspace.mkdir()
    session = SimpleNamespace(workspace=str(workspace), workspace_mode="external")

    assert resolve_session_workspace(
        session, str(workspace), requested_is_trusted=True
    ) == workspace.resolve()


def test_chat_start_uses_default_workspace_for_unverified_cron_session(tmp_path, monkeypatch):
    workspace = tmp_path / "unverified-workspace"
    default_workspace = tmp_path / "default-workspace"
    workspace.mkdir()
    default_workspace.mkdir()
    import api.config as config

    monkeypatch.setattr(config, "DEFAULT_WORKSPACE", default_workspace)
    session = SimpleNamespace(
        workspace=str(workspace),
        workspace_mode="external",
        workspace_state="workspace_unverified",
        source_tag="cron",
    )

    assert routes._resolve_chat_workspace_with_recovery(session, None) == str(default_workspace.resolve())

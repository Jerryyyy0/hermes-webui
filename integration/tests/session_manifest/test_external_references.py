import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

from api.models import Session
from integration.session_manifest.manifest import build_session_manifest, extract_turn_artifact_entries_for_manifest
from integration.session_manifest.store import upsert_manifest_records
from integration.session_manifest.external_references.policy import open_external_regular_file
from integration.session_manifest.external_references.preview import serve_registered_external_artifact
from integration.session_manifest.external_references.references import registered_external_artifact
from integration.workspace.handlers import try_handle_get


def _patch_external_state(monkeypatch, state_dir: Path) -> None:
    monkeypatch.setattr('integration.session_manifest.store.STATE_DIR', state_dir)
    monkeypatch.setattr('integration.session_manifest.external_references.policy.STATE_DIR', state_dir)
    monkeypatch.setattr('integration.session_manifest.external_references.references.STATE_DIR', state_dir)


def _persist_external_artifact(state_dir: Path, workspace: Path, external: Path) -> None:
    session = Session(session_id='externalartifact01', workspace=str(workspace), messages=[])
    persisted = upsert_manifest_records(
        session,
        'turn:0',
        [{'path': external.as_posix(), 'source_tool': 'assistant_prose', 'preview': 'file'}],
    )
    assert persisted[0]['path'] == external.as_posix()
    assert (state_dir / 'session_manifest.db').is_file()


def test_registered_external_artifact_opens_only_regular_non_symlink_file(tmp_path, monkeypatch):
    state_dir = tmp_path / 'state'
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    external = tmp_path / 'exports' / 'report.txt'
    external.parent.mkdir()
    external.write_text('external report', encoding='utf-8')
    link = tmp_path / 'exports' / 'report-link.txt'
    link.symlink_to(external)
    _patch_external_state(monkeypatch, state_dir)
    _persist_external_artifact(state_dir, workspace, external)

    assert registered_external_artifact(external) is not None
    opened = open_external_regular_file(external)
    assert opened is not None
    fd, _path, _stat = opened
    os.close(fd)
    assert open_external_regular_file(link) is None
    protected = state_dir / 'sessions' / 'private.txt'
    protected.parent.mkdir(parents=True)
    protected.write_text('private', encoding='utf-8')
    assert open_external_regular_file(protected) is None


def test_registered_session_attachment_can_use_external_artifact_preview(tmp_path, monkeypatch):
    state_dir = tmp_path / 'state'
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    attachment = state_dir / 'attachments' / 'attachment-session-01' / 'source.pdf'
    attachment.parent.mkdir(parents=True)
    attachment.write_bytes(b'%PDF-1.4 attachment')
    _patch_external_state(monkeypatch, state_dir)
    _persist_external_artifact(state_dir, workspace, attachment)

    assert registered_external_artifact(attachment) is not None
    opened = open_external_regular_file(attachment)
    assert opened is not None
    fd, _path, _stat = opened
    os.close(fd)

    handler = MagicMock()

    def serve_and_close(*args, **kwargs):
        os.close(kwargs['opened_fd'])
        return True

    with patch('api.routes._serve_file_bytes', side_effect=serve_and_close) as serve:
        assert serve_registered_external_artifact(handler, attachment.as_posix()) is True
    assert serve.call_args.args[1] == attachment

    unregistered = attachment.parent / 'not-an-artifact.pdf'
    unregistered.write_bytes(b'%PDF-1.4 unregistered')
    assert registered_external_artifact(unregistered) is None


def test_registered_hermes_memory_can_use_external_artifact_preview(tmp_path, monkeypatch):
    state_dir = tmp_path / 'state'
    hermes_home = tmp_path / 'hermes-home'
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    memory = hermes_home / 'memories' / 'project-notes.md'
    memory.parent.mkdir(parents=True)
    memory.write_text('# Project notes', encoding='utf-8')
    _patch_external_state(monkeypatch, state_dir)
    monkeypatch.setenv('HERMES_HOME', hermes_home.as_posix())
    _persist_external_artifact(state_dir, workspace, memory)

    assert registered_external_artifact(memory) is not None
    opened = open_external_regular_file(memory)
    assert opened is not None
    fd, _path, _stat = opened
    os.close(fd)

    handler = MagicMock()

    def serve_and_close(*args, **kwargs):
        os.close(kwargs['opened_fd'])
        return True

    with patch('api.routes._serve_file_bytes', side_effect=serve_and_close) as serve:
        assert serve_registered_external_artifact(handler, memory.as_posix()) is True
    assert serve.call_args.args[1] == memory


def test_successful_mutation_tool_keeps_external_absolute_output(tmp_path, monkeypatch):
    state_dir = tmp_path / 'state'
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    external = tmp_path / 'exports' / 'report.txt'
    external.parent.mkdir()
    external.write_text('external report', encoding='utf-8')
    _patch_external_state(monkeypatch, state_dir)
    session = Session(
        session_id='externalartifact-tool01',
        workspace=str(workspace),
        messages=[
            {'role': 'user', 'content': '生成报告', '_turn_key': 'turn:0'},
            {
                'role': 'assistant',
                'tool_calls': [{
                    'id': 'write-1',
                    'function': {
                        'name': 'write_file',
                        'arguments': json.dumps({'path': external.as_posix()}),
                    },
                }],
            },
            {'role': 'tool', 'tool_call_id': 'write-1', 'content': 'ok'},
        ],
    )

    assert extract_turn_artifact_entries_for_manifest(session, 'turn:0') == [{
        'path': external.as_posix(),
        'source_tool': 'write_file',
        'preview': 'file',
    }]


def test_external_preview_uses_registered_path_and_existing_preview_url(tmp_path, monkeypatch):
    state_dir = tmp_path / 'state'
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    external = tmp_path / 'exports' / 'report.txt'
    external.parent.mkdir()
    external.write_text('external report', encoding='utf-8')
    _patch_external_state(monkeypatch, state_dir)
    _persist_external_artifact(state_dir, workspace, external)

    handler = MagicMock()

    def serve_and_close(*args, **kwargs):
        os.close(kwargs['opened_fd'])
        return True

    with patch('api.routes._serve_file_bytes', side_effect=serve_and_close) as serve:
        assert serve_registered_external_artifact(handler, external.as_posix()) is True
    assert serve.call_args.args[1] == external
    assert serve.call_args.kwargs['opened_fd'] is not None

    request_handler = MagicMock()
    parsed = urlparse(f'/api/integration/workspace/file?path={external.as_posix()}')
    # The handler imports the helper lazily, so patch its implementation.
    with patch(
        'integration.session_manifest.external_references.preview.serve_registered_external_artifact',
        return_value=True,
    ) as preview_serve:
        with patch('integration.workspace.handlers.integration_enabled', return_value=True):
            assert try_handle_get(request_handler, parsed) is True
    preview_serve.assert_called_once_with(request_handler, external.as_posix())

    unregistered = tmp_path / 'exports' / 'unregistered.txt'
    unregistered.write_text('not registered', encoding='utf-8')
    denied_handler = MagicMock()
    denied_parsed = urlparse(f'/api/integration/workspace/file?path={unregistered.as_posix()}')
    with patch('integration.workspace.handlers.integration_enabled', return_value=True):
        assert try_handle_get(denied_handler, denied_parsed) is True
    denied_handler.send_response.assert_called_with(404)


def test_persisted_external_artifact_is_live_and_becomes_expired_only_when_unavailable(tmp_path, monkeypatch):
    state_dir = tmp_path / 'state'
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    external = tmp_path / 'exports' / 'report.txt'
    external.parent.mkdir()
    external.write_text('version one', encoding='utf-8')
    _patch_external_state(monkeypatch, state_dir)
    session = Session(
        session_id='externalartifact02',
        workspace=str(workspace),
        messages=[
            {'role': 'user', 'content': '生成报告', '_turn_key': 'turn:0'},
            {'role': 'assistant', 'content': f'已生成 {external.as_posix()}'},
        ],
    )
    persisted = upsert_manifest_records(
        session,
        'turn:0',
        [{'path': external.as_posix(), 'source_tool': 'assistant_prose', 'preview': 'file'}],
    )
    assert persisted

    with patch('integration.session_manifest.manifest._load_display_messages', lambda s: list(s.messages)):
        manifest = build_session_manifest(session)
    assert manifest['artifacts'] == [{
        'path': external.as_posix(),
        'preview': 'file',
        'source_tool': 'assistant_prose',
    }]

    external.unlink()
    with patch('integration.session_manifest.manifest._load_display_messages', lambda s: list(s.messages)):
        expired_manifest = build_session_manifest(session)
    assert expired_manifest['artifacts'] == [{
        'path': external.as_posix(),
        'preview': 'file',
        'source_tool': 'assistant_prose',
        'status': 'expired',
    }]

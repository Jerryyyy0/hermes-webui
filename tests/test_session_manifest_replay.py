"""Regression coverage for replayed tool executions in session manifests."""

import json

from api.models import Session
from api.session_manifest import build_session_manifest


def _write_call(tid: str, path: str) -> dict:
    return {
        'role': 'assistant',
        'content': '',
        'tool_calls': [{
            'id': tid,
            'function': {'name': 'write_file', 'arguments': json.dumps({'path': path})},
        }],
    }


def test_replayed_write_call_belongs_only_to_latest_turn(tmp_path, monkeypatch):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    (workspace / 'report.html').write_text('<html></html>', encoding='utf-8')
    session = Session(
        session_id='manifest_replay_turn01',
        workspace=str(workspace),
        messages=[
            {'role': 'user', 'content': 'research', '_turn_key': 'turn:2', 'timestamp': 10},
            {**_write_call('call-html', 'report.html'), 'timestamp': 11},
            {'role': 'tool', 'tool_call_id': 'call-html', 'name': 'write_file', 'content': 'saved', 'timestamp': 12},
            {'role': 'user', 'content': 'export', '_turn_key': 'turn:3', 'timestamp': 20},
            {**_write_call('call-html', 'report.html'), 'timestamp': 20.000001},
            {'role': 'tool', 'tool_call_id': 'call-html', 'name': 'write_file', 'content': 'saved', 'timestamp': 20.000002},
        ],
        tool_calls=[],
    )
    monkeypatch.setattr('api.session_manifest._load_display_messages', lambda s: list(s.messages))
    monkeypatch.setattr('api.session_manifest_store.load_manifest_records', lambda *a, **k: [])
    monkeypatch.setattr('api.session_manifest_store.load_manifest_decided_turn_keys', lambda *a, **k: set())

    manifest = build_session_manifest(session)

    assert [row['path'] for row in manifest['artifacts']] == ['report.html']
    artifacts_by_turn = {turn['turn_key']: [row['path'] for row in turn['artifacts']] for turn in manifest['turns']}
    assert artifacts_by_turn == {'turn:2': [], 'turn:3': ['report.html']}


def test_distinct_write_calls_to_same_path_remain_in_each_turn(tmp_path, monkeypatch):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    (workspace / 'report.html').write_text('<html></html>', encoding='utf-8')
    session = Session(
        session_id='manifest_distinct_turns01',
        workspace=str(workspace),
        messages=[
            {'role': 'user', 'content': 'draft', '_turn_key': 'turn:1'},
            _write_call('call-draft', 'report.html'),
            {'role': 'tool', 'tool_call_id': 'call-draft', 'name': 'write_file', 'content': 'saved'},
            {'role': 'user', 'content': 'update', '_turn_key': 'turn:2'},
            _write_call('call-update', 'report.html'),
            {'role': 'tool', 'tool_call_id': 'call-update', 'name': 'write_file', 'content': 'saved'},
        ],
        tool_calls=[],
    )
    monkeypatch.setattr('api.session_manifest._load_display_messages', lambda s: list(s.messages))
    monkeypatch.setattr('api.session_manifest_store.load_manifest_records', lambda *a, **k: [])
    monkeypatch.setattr('api.session_manifest_store.load_manifest_decided_turn_keys', lambda *a, **k: set())

    manifest = build_session_manifest(session)

    assert all(turn['artifacts'][0]['path'] == 'report.html' for turn in manifest['turns'])

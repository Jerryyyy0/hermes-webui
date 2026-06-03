"""Tests for session manifest extraction (todos, artifacts, references)."""

import json
import urllib.request
from pathlib import Path

import pytest

from tests._pytest_port import BASE
from api.models import Session
from api.session_manifest import (
    ToolEvent,
    _collect_tool_events,
    _extract_artifacts_and_references,
    _extract_latest_todos,
    _normalize_manifest_path,
    _resolve_manifest_path,
    build_session_manifest,
    extract_manifest_delta_from_tool_event,
    merge_manifest_delta,
)


def test_normalize_manifest_path_strips_noise():
    assert _normalize_manifest_path('`src/app.py`') == 'src/app.py'
    assert _normalize_manifest_path('./docs/readme.md') == 'docs/readme.md'
    assert _normalize_manifest_path('node_modules/pkg/index.js') == ''


def test_resolve_manifest_path_absolute_under_workspace(tmp_path):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    report = workspace / 'langchain_langgraph_report_2025.md'
    report.write_text('# report', encoding='utf-8')
    external = tmp_path / 'outside.md'
    external.write_text('# outside', encoding='utf-8')
    absolute = str(report)
    assert _resolve_manifest_path(workspace, absolute) == 'langchain_langgraph_report_2025.md'
    assert _resolve_manifest_path(workspace, 'langchain_langgraph_report_2025.md') == (
        'langchain_langgraph_report_2025.md'
    )
    assert _resolve_manifest_path(workspace, str(external)) == external.resolve().as_posix()


def test_extract_latest_todos_from_tool_message():
    messages = [
        {'role': 'user', 'content': 'plan'},
        {
            'role': 'tool',
            'content': json.dumps({
                'todos': [
                    {'id': '1', 'content': 'First', 'status': 'pending'},
                    {'id': '2', 'content': 'Second', 'status': 'in_progress'},
                ],
            }),
            'timestamp': 10,
        },
        {
            'role': 'tool',
            'content': json.dumps({
                'todos': [
                    {'id': '1', 'content': 'First', 'status': 'completed'},
                ],
            }),
            'timestamp': 20,
        },
    ]
    latest = _extract_latest_todos(messages)
    assert latest['items'] == [
        {'id': '1', 'content': 'First', 'status': 'completed'},
        {'id': '2', 'content': 'Second', 'status': 'in_progress'},
    ]
    assert latest['source_tool_msg_idx'] == 2


def test_collect_tool_events_links_assistant_and_tool_messages():
    messages = [
        {
            'role': 'assistant',
            'content': '',
            'tool_calls': [{
                'id': 'call-1',
                'function': {'name': 'write_file', 'arguments': '{"path":"out.txt"}'},
            }],
        },
        {'role': 'tool', 'tool_call_id': 'call-1', 'content': 'saved'},
    ]
    events = _collect_tool_events(messages, [])
    assert len(events) == 1
    assert events[0].name == 'write_file'
    assert events[0].args['path'] == 'out.txt'
    assert events[0].result == 'saved'


def test_extract_artifacts_and_references(tmp_path):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    events = [
        ToolEvent(name='write_file', args={'path': 'src/new.py'}, assistant_msg_idx=1),
        ToolEvent(name='read_file', args={'path': 'README.md'}, result='hello', assistant_msg_idx=3),
        ToolEvent(name='list_dir', args={'path': 'src'}, assistant_msg_idx=5),
    ]
    artifacts, references = _extract_artifacts_and_references(events, workspace)
    assert [row['path'] for row in artifacts] == ['src/new.py']
    ref_paths = {row['path']: row['kind'] for row in references}
    assert ref_paths['README.md'] == 'file'
    assert ref_paths['src'] == 'dir'


def test_build_session_manifest_persists_workspace_files(tmp_path, monkeypatch):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    target = workspace / 'notes.txt'
    target.write_text('hello', encoding='utf-8')

    sid = 'manifest01test'
    session = Session(
        session_id=sid,
        workspace=str(workspace),
        messages=[
            {
                'role': 'assistant',
                'tool_calls': [{
                    'id': 'c1',
                    'function': {'name': 'write_file', 'arguments': '{"path":"notes.txt"}'},
                }],
            },
            {'role': 'tool', 'tool_call_id': 'c1', 'content': 'ok'},
            {
                'role': 'assistant',
                'tool_calls': [{
                    'id': 'c2',
                    'function': {'name': 'read_file', 'arguments': '{"path":"notes.txt"}'},
                }],
            },
            {'role': 'tool', 'tool_call_id': 'c2', 'content': target.read_text(encoding='utf-8')},
        ],
        tool_calls=[],
    )
    session.save()

    monkeypatch.setattr('api.models.SESSION_DIR', tmp_path)
    monkeypatch.setattr('api.models.get_state_db_session_messages', lambda *a, **k: [])
    monkeypatch.setattr(
        'api.session_manifest._load_display_messages',
        lambda s: list(s.messages),
    )

    manifest = build_session_manifest(session)
    assert manifest['session_id'] == sid
    assert manifest['counts']['artifacts'] == 1
    assert manifest['counts']['references'] == 1
    artifact = manifest['artifacts'][0]
    assert artifact['path'] == 'notes.txt'
    assert artifact['preview']['exists'] is True
    assert artifact['preview']['previewable'] is True


def test_build_session_manifest_resolves_absolute_write_path(tmp_path, monkeypatch):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    target = workspace / 'langchain_langgraph_report_2025.md'
    target.write_text('# report', encoding='utf-8')
    sid = 'manifestabs01'
    session = Session(
        session_id=sid,
        workspace=str(workspace),
        messages=[
            {
                'role': 'assistant',
                'tool_calls': [{
                    'id': 'c1',
                    'function': {
                        'name': 'write_file',
                        'arguments': json.dumps({'path': str(target)}),
                    },
                }],
            },
            {'role': 'tool', 'tool_call_id': 'c1', 'content': 'ok'},
        ],
        tool_calls=[],
    )
    session.save()
    monkeypatch.setattr('api.models.SESSION_DIR', tmp_path)
    monkeypatch.setattr('api.models.get_state_db_session_messages', lambda *a, **k: [])
    monkeypatch.setattr(
        'api.session_manifest._load_display_messages',
        lambda s: list(s.messages),
    )
    manifest = build_session_manifest(session)
    artifact = manifest['artifacts'][0]
    assert artifact['path'] == 'langchain_langgraph_report_2025.md'
    assert artifact['preview']['workspace_relative_path'] == 'langchain_langgraph_report_2025.md'
    assert artifact['preview']['exists'] is True
    assert artifact['preview']['previewable'] is True


def test_build_session_manifest_includes_external_write_path(tmp_path, monkeypatch):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    external = tmp_path / 'outside' / 'report.md'
    external.parent.mkdir()
    external.write_text('# external report', encoding='utf-8')
    sid = 'manifestext01'
    session = Session(
        session_id=sid,
        workspace=str(workspace),
        messages=[
            {
                'role': 'assistant',
                'tool_calls': [{
                    'id': 'c1',
                    'function': {
                        'name': 'write_file',
                        'arguments': json.dumps({'path': str(external)}),
                    },
                }],
            },
            {'role': 'tool', 'tool_call_id': 'c1', 'content': 'ok'},
        ],
        tool_calls=[],
    )
    session.save()
    monkeypatch.setattr('api.models.SESSION_DIR', tmp_path)
    monkeypatch.setattr('api.models.get_state_db_session_messages', lambda *a, **k: [])
    monkeypatch.setattr(
        'api.session_manifest._load_display_messages',
        lambda s: list(s.messages),
    )
    manifest = build_session_manifest(session)
    artifact = manifest['artifacts'][0]
    assert artifact['path'] == external.resolve().as_posix()
    assert artifact['preview']['absolute_path'] == external.resolve().as_posix()
    assert artifact['preview']['in_workspace'] is False
    assert artifact['preview']['exists'] is True
    assert artifact['preview']['size'] == len('# external report')
    assert artifact['preview']['previewable'] is False


def test_session_manifest_route(cleanup_test_sessions):
    workspace = Path(__file__).parent / 'fixtures'
    workspace.mkdir(exist_ok=True)
    data = json.dumps({'workspace': str(workspace.resolve())}).encode()
    req = urllib.request.Request(
        BASE + '/api/session/new',
        data=data,
        headers={'Content-Type': 'application/json'},
    )
    with urllib.request.urlopen(req, timeout=10) as response:
        d = json.loads(response.read())
    sid = d['session']['session_id']
    with urllib.request.urlopen(BASE + f'/api/session/manifest?session_id={sid}', timeout=10) as response:
        payload = json.loads(response.read())
        assert response.status == 200
    manifest = payload['manifest']
    assert manifest['session_id'] == sid
    assert manifest['counts']['todos'] == 0
    assert isinstance(manifest['turns'], list)


def test_build_session_manifest_groups_artifacts_by_turn(tmp_path, monkeypatch):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    (workspace / 'first.txt').write_text('one', encoding='utf-8')
    (workspace / 'second.txt').write_text('two', encoding='utf-8')
    session = Session(
        session_id='manifestturns01',
        workspace=str(workspace),
        messages=[
            {'role': 'user', 'content': 'first'},
            {
                'role': 'assistant',
                'tool_calls': [{
                    'id': 'c1',
                    'function': {'name': 'write_file', 'arguments': json.dumps({'path': 'first.txt'})},
                }],
            },
            {'role': 'tool', 'tool_call_id': 'c1', 'content': 'ok'},
            {'role': 'user', 'content': 'second'},
            {
                'role': 'assistant',
                'tool_calls': [{
                    'id': 'c2',
                    'function': {'name': 'write_file', 'arguments': json.dumps({'path': 'second.txt'})},
                }],
            },
            {'role': 'tool', 'tool_call_id': 'c2', 'content': 'ok'},
        ],
        tool_calls=[],
    )
    monkeypatch.setattr('api.session_manifest._load_display_messages', lambda s: list(s.messages))

    manifest = build_session_manifest(session)

    assert manifest['counts']['turns'] == 2
    first_turn, second_turn = manifest['turns']
    assert first_turn['turn_key'] == 'turn:0'
    assert [row['path'] for row in first_turn['artifacts']] == ['first.txt']
    assert second_turn['turn_key'] == 'turn:3'
    assert [row['path'] for row in second_turn['artifacts']] == ['second.txt']
    assert {row['path'] for row in manifest['artifacts']} == {'first.txt', 'second.txt'}


def test_build_session_manifest_merges_partial_todos_after_done(tmp_path, monkeypatch):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    session = Session(
        session_id='manifesttodos01',
        workspace=str(workspace),
        messages=[
            {'role': 'user', 'content': 'plan'},
            {
                'role': 'tool',
                'content': json.dumps({
                    'todos': [
                        {'id': '1', 'content': 'Research LangChain', 'status': 'in_progress'},
                        {'id': '2', 'content': 'Research LangGraph', 'status': 'pending'},
                        {'id': '3', 'content': 'Write report', 'status': 'pending'},
                    ],
                }),
            },
            {
                'role': 'tool',
                'content': json.dumps({
                    'todos': [
                        {'id': '2', 'content': '(no description)', 'status': 'completed'},
                        {'id': '3', 'status': 'completed'},
                    ],
                }),
            },
        ],
        tool_calls=[],
    )
    monkeypatch.setattr('api.session_manifest._load_display_messages', lambda s: list(s.messages))

    manifest = build_session_manifest(session)

    assert manifest['todos']['items'] == [
        {'id': '1', 'content': 'Research LangChain', 'status': 'in_progress'},
        {'id': '2', 'content': 'Research LangGraph', 'status': 'completed'},
        {'id': '3', 'content': 'Write report', 'status': 'completed'},
    ]


def test_discovery_tools_do_not_become_references(tmp_path):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    events = [
        ToolEvent(name='rg', args={'path': 'src'}, result='src/app.py:match'),
        ToolEvent(name='semantic_search', args={'target_directories': ['docs']}, result='docs/spec.md'),
    ]

    _artifacts, references = _extract_artifacts_and_references(events, workspace)

    assert references == []


def test_manifest_delta_extracts_todo_and_artifact(tmp_path):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    target = workspace / 'notes.txt'
    target.write_text('hello', encoding='utf-8')
    todo_event = ToolEvent(
        name='todo',
        result=json.dumps({'todos': [{'id': 't1', 'content': 'Ship', 'status': 'done-ish'}]}),
        tid='todo-call',
        status='completed',
    )
    write_event = ToolEvent(
        name='write_file',
        args={'path': 'notes.txt'},
        tid='write-call',
        status='in_progress',
    )

    todo_delta = extract_manifest_delta_from_tool_event(
        todo_event, workspace, session_id='sid', stream_id='stream1', turn_key='live:stream1', sequence=1,
    )
    write_delta = extract_manifest_delta_from_tool_event(
        write_event, workspace, session_id='sid', stream_id='stream1', turn_key='live:stream1', sequence=2,
    )

    assert todo_delta['todos']['items'] == [{'id': 't1', 'content': 'Ship', 'status': 'unknown'}]
    assert write_delta['artifacts'][0]['path'] == 'notes.txt'
    assert write_delta['artifacts'][0]['status'] == 'in_progress'


def test_merge_manifest_delta_is_idempotent_by_path():
    base = {'session_id': 'sid', 'todos': {'items': []}, 'artifacts': [], 'references': [], 'turns': []}
    delta = {
        'session_id': 'sid',
        'stream_id': 'stream1',
        'turn_key': 'turn:0',
        'sequence': 1,
        'artifacts': [{'path': 'notes.txt', 'source_tool': 'write_file'}],
        'references': [],
    }

    merged = merge_manifest_delta(merge_manifest_delta(base, delta), delta)

    assert [row['path'] for row in merged['artifacts']] == ['notes.txt']
    assert len(merged['turns']) == 1
    assert [row['path'] for row in merged['turns'][0]['artifacts']] == ['notes.txt']


def test_merge_manifest_delta_merges_partial_todo_updates_by_id():
    base = {
        'session_id': 'sid',
        'todos': {
            'items': [
                {'id': '1', 'content': 'Research LangChain', 'status': 'in_progress'},
                {'id': '2', 'content': 'Research LangGraph', 'status': 'pending'},
                {'id': '3', 'content': 'Write report', 'status': 'pending'},
            ],
        },
        'artifacts': [],
        'references': [],
        'turns': [],
    }
    delta = {
        'session_id': 'sid',
        'stream_id': 'stream1',
        'turn_key': 'turn:0',
        'todos': {
            'items': [
                {'id': '2', 'content': '(no description)', 'status': 'completed'},
                {'id': '3', 'status': 'completed'},
            ],
            'mode': 'replace_latest',
        },
    }

    merged = merge_manifest_delta(base, delta)

    assert merged['todos']['items'] == [
        {'id': '1', 'content': 'Research LangChain', 'status': 'in_progress'},
        {'id': '2', 'content': 'Research LangGraph', 'status': 'completed'},
        {'id': '3', 'content': 'Write report', 'status': 'completed'},
    ]

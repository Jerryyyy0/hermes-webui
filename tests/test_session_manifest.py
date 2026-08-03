"""Tests for session manifest extraction (todos, artifacts, references)."""

import copy
import json
import urllib.request
from pathlib import Path

import pytest

from tests._pytest_port import BASE
from api.models import Session
from api.session_manifest import (
    MANIFEST_PREVIEW_FILE,
    MANIFEST_PREVIEW_SKILL,
    MEDIA_ARTIFACT_SOURCE,
    ASSISTANT_PROSE_ARTIFACT_SOURCE,
    TURN_RECONCILE_SOURCE,
    ToolEvent,
    _apply_public_todos_to_manifest_delta,
    _collect_media_artifact_events,
    _collect_tool_events,
    _ensure_turn_keys,
    _extract_artifacts_and_references,
    _extract_latest_todos,
    _extract_manifest_records,
    _extract_turn_artifact_paths,
    _message_turns,
    _next_turn_key,
    _normalize_manifest_path,
    _paths_from_assistant_media,
    _paths_from_last_assistant_message,
    _public_todo_items,
    _resolve_manifest_path,
    _rows_to_wire,
    _serialize_manifest_row,
    _wire_todos,
    build_session_manifest,
    extract_manifest_delta_from_assistant_media,
    extract_manifest_delta_from_turn_reconcile,
    extract_manifest_delta_from_tool_event,
    filter_existing_turn_artifact_paths,
    merge_manifest_delta,
    turn_artifacts_for_wire,
    _turn_message_slice,
)


@pytest.fixture(autouse=True)
def _isolate_manifest_store(tmp_path, monkeypatch):
    monkeypatch.setattr('api.session_manifest_store.STATE_DIR', tmp_path / 'state')


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
    assert references == []


def test_serialize_manifest_row_shape():
    row = _serialize_manifest_row('docs/a.md', MANIFEST_PREVIEW_FILE, 'read_file')
    assert row == {
        'path': 'docs/a.md',
        'preview': 'file',
        'source_tool': 'read_file',
    }


def test_serialize_manifest_row_includes_profile_when_set():
    row = _serialize_manifest_row(
        'docs/a.md', MANIFEST_PREVIEW_FILE, 'write_file', profile='ops',
    )
    assert row == {
        'path': 'docs/a.md',
        'preview': 'file',
        'source_tool': 'write_file',
        'profile': 'ops',
    }


def test_serialize_manifest_row_includes_status_when_set():
    row = _serialize_manifest_row(
        'docs/a.md', MANIFEST_PREVIEW_FILE, 'write_file', status='expired',
    )
    assert row['status'] == 'expired'


def test_build_session_manifest_marks_deleted_artifact_expired(tmp_path, monkeypatch):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    target = workspace / 'notes.txt'
    target.write_text('hello', encoding='utf-8')
    session = Session(
        session_id='manifestexp01',
        workspace=str(workspace),
        messages=[
            {'role': 'user', 'content': 'write notes', '_turn_key': 'turn:0'},
            {
                'role': 'assistant',
                'tool_calls': [{
                    'id': 'c1',
                    'function': {'name': 'write_file', 'arguments': '{"path":"notes.txt"}'},
                }],
            },
            {'role': 'tool', 'tool_call_id': 'c1', 'content': 'ok'},
        ],
        tool_calls=[],
        turn_artifacts={
            'turn:0': [{'path': 'notes.txt', 'source_tool': 'write_file'}],
        },
    )
    monkeypatch.setattr('api.session_manifest._load_display_messages', lambda s: list(s.messages))
    target.unlink()
    manifest = build_session_manifest(session)
    assert manifest['artifacts'] == [{
        'path': 'notes.txt',
        'preview': 'file',
        'source_tool': 'write_file',
        'status': 'expired',
    }]
    assert manifest['turns'][0]['artifacts'][0]['status'] == 'expired'


def test_rows_to_wire_references_drops_missing_file_rows(tmp_path):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    rows = [{
        'path': 'missing.txt',
        'source_tool': 'read_file',
        'kind': 'file',
        'entry_kind': 'file',
    }]
    wired = _rows_to_wire(rows, workspace, collection='references')
    assert wired == []


def test_rows_to_wire_references_drops_existing_file_rows(tmp_path):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    target = workspace / 'notes.txt'
    target.write_text('hello', encoding='utf-8')
    rows = [{
        'path': 'notes.txt',
        'source_tool': 'read_file',
        'kind': 'file',
        'entry_kind': 'file',
    }]
    wired = _rows_to_wire(rows, workspace, collection='references')
    assert wired == []


def test_rows_to_wire_artifacts_require_turn_key_for_expired(tmp_path):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    rows = [{
        'path': 'tmp_fix.py',
        'source_tool': 'write_file',
        'kind': 'artifact',
        'entry_kind': 'file',
        # no turn_key -> should not emit expired
    }]
    wired = _rows_to_wire(rows, workspace, collection='artifacts')
    assert wired == []


def test_build_session_manifest_drops_unattributed_expired_candidate(tmp_path, monkeypatch):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    session = Session(
        session_id='manifestexp02',
        workspace=str(workspace),
        messages=[
            {'role': 'user', 'content': 'q1', '_turn_key': 'turn:1'},
            {'role': 'assistant', 'content': 'a1'},
        ],
        tool_calls=[],
    )

    monkeypatch.setattr('api.session_manifest._load_display_messages', lambda s: list(s.messages))

    def _fake_extract(events, ws, messages=None, *, skills_dir=None):
        return (
            [{
                'path': 'tmp_fix.py',
                'source_tool': 'write_file',
                'kind': 'artifact',
                'entry_kind': 'file',
                # deliberately no turn_key/provenance
            }],
            [],
            [{
                'turn_key': 'turn:1',
                'user_msg_idx': 0,
                'start_msg_idx': 0,
                'end_msg_idx': 1,
                'artifacts': [],
                'references': [],
            }],
        )

    monkeypatch.setattr('api.session_manifest._extract_manifest_records', _fake_extract)
    manifest = build_session_manifest(session)
    assert manifest['artifacts'] == []
    assert manifest['turns'][0]['artifacts'] == []


def test_build_session_manifest_artifacts_include_session_profile(tmp_path, monkeypatch):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    target = workspace / 'notes.txt'
    target.write_text('hello', encoding='utf-8')
    session = Session(
        session_id='manifestprof01',
        workspace=str(workspace),
        profile='ops',
        messages=[
            {'role': 'user', 'content': 'write notes'},
            {
                'role': 'assistant',
                'tool_calls': [{
                    'id': 'c1',
                    'function': {'name': 'write_file', 'arguments': '{"path":"notes.txt"}'},
                }],
            },
            {'role': 'tool', 'tool_call_id': 'c1', 'content': 'ok'},
        ],
        tool_calls=[],
    )
    monkeypatch.setattr('api.session_manifest._load_display_messages', lambda s: list(s.messages))
    manifest = build_session_manifest(session)
    assert manifest['artifacts'][0]['profile'] == 'ops'
    assert manifest['turns'][0]['artifacts'][0]['profile'] == 'ops'
    assert all('profile' not in row for row in manifest['references'])


def test_build_session_manifest_omits_profile_when_session_has_none(tmp_path, monkeypatch):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    (workspace / 'notes.txt').write_text('hello', encoding='utf-8')
    session = Session(
        session_id='manifestprof02',
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
        ],
        tool_calls=[],
    )
    monkeypatch.setattr('api.session_manifest._load_display_messages', lambda s: list(s.messages))
    manifest = build_session_manifest(session)
    assert 'profile' not in manifest['artifacts'][0]


def test_merge_manifest_delta_preserves_profile():
    base = {'todos': {'items': []}, 'artifacts': [], 'references': [], 'turns': []}
    delta = {
        'artifacts': [{
            'path': 'notes.txt',
            'preview': 'file',
            'source_tool': 'write_file',
            'profile': 'ops',
        }],
    }
    merged = merge_manifest_delta(base, delta)
    assert merged['artifacts'][0]['profile'] == 'ops'


def test_merge_manifest_delta_keeps_same_path_for_distinct_profiles():
    base = {
        'todos': {'items': []},
        'artifacts': [{
            'path': 'notes.txt',
            'preview': 'file',
            'source_tool': 'write_file',
            'profile': 'ops',
        }],
        'references': [],
        'turns': [],
    }
    delta = {
        'artifacts': [{
            'path': 'notes.txt',
            'preview': 'file',
            'source_tool': 'write_file',
            'profile': 'research',
        }],
    }
    merged = merge_manifest_delta(base, delta)
    assert [(row['path'], row.get('profile')) for row in merged['artifacts']] == [
        ('notes.txt', 'ops'),
        ('notes.txt', 'research'),
    ]


def test_rows_to_wire_keeps_same_path_for_distinct_profiles(tmp_path):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    (workspace / 'notes.txt').write_text('hello', encoding='utf-8')
    rows = [
        {'path': 'notes.txt', 'source_tool': 'write_file', 'profile': 'ops'},
        {'path': 'notes.txt', 'source_tool': 'write_file', 'profile': 'research'},
    ]

    wire = _rows_to_wire(rows, workspace)

    assert [(row['path'], row.get('profile')) for row in wire] == [
        ('notes.txt', 'ops'),
        ('notes.txt', 'research'),
    ]


def test_extract_manifest_delta_from_tool_event_includes_profile(tmp_path):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    (workspace / 'notes.txt').write_text('hello', encoding='utf-8')
    write_event = ToolEvent(
        name='write_file',
        args={'path': 'notes.txt'},
        assistant_msg_idx=1,
    )
    delta = extract_manifest_delta_from_tool_event(
        write_event,
        workspace,
        turn_key='turn:0',
        default_profile='ops',
    )
    assert delta['artifacts'][0]['profile'] == 'ops'


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
    assert 'session_id' not in manifest
    assert 'counts' not in manifest
    assert len(manifest['artifacts']) == 1
    assert len(manifest['references']) == 0
    artifact = manifest['artifacts'][0]
    assert artifact == {
        'path': 'notes.txt',
        'preview': 'file',
        'source_tool': 'write_file',
    }
    for turn in manifest['turns']:
        assert all(row.get('path') != 'notes.txt' for row in turn.get('references') or [])


def test_build_session_manifest_prefers_store_artifacts(tmp_path, monkeypatch):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    target = workspace / 'notes.txt'
    target.write_text('hello', encoding='utf-8')

    session = Session(
        session_id='manifeststore01',
        workspace=str(workspace),
        profile='ops',
        messages=[
            {'role': 'user', 'content': 'write notes', '_turn_key': 'turn:1'},
            {
                'role': 'assistant',
                'tool_calls': [{
                    'id': 'c1',
                    'function': {'name': 'write_file', 'arguments': '{"path":"notes.txt"}'},
                }],
            },
            {'role': 'tool', 'tool_call_id': 'c1', 'content': 'ok'},
        ],
        tool_calls=[],
    )
    monkeypatch.setattr('api.session_manifest._load_display_messages', lambda s: list(s.messages))
    monkeypatch.setattr(
        'api.session_manifest_store.load_manifest_records',
        lambda s, include_lineage=True: [{
            'session_id': s.session_id,
            'lineage_key': s.session_id,
            'profile': 'ops',
            'turn_key': 'turn:1',
            'record_kind': 'artifact',
            'path': 'notes.txt',
            'preview': 'file',
            'source_tool': 'media',
        }],
    )

    manifest = build_session_manifest(session)

    assert manifest['artifacts'] == [{
        'path': 'notes.txt',
        'preview': 'file',
        'source_tool': 'media',
        'profile': 'ops',
    }]
    assert manifest['turns'][0]['artifacts'] == manifest['artifacts']


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
    assert artifact['preview'] == 'file'


def test_build_session_manifest_excludes_external_write_path(tmp_path, monkeypatch):
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
    assert manifest['artifacts'] == []


def test_profile_memory_files_are_excluded_from_manifest(tmp_path, monkeypatch):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    profile_home = tmp_path / 'profile-home'
    profile_home.mkdir()
    mem_dir = profile_home / 'memories'
    mem_dir.mkdir()
    memory_file = mem_dir / 'MEMORY.md'
    memory_file.write_text('# notes', encoding='utf-8')

    session = Session(
        session_id='manifestmem01',
        workspace=str(workspace),
        profile='test-profile',
        messages=[
            {
                'role': 'assistant',
                'tool_calls': [{
                    'id': 'c1',
                    'function': {
                        'name': 'read_file',
                        'arguments': json.dumps({'path': str(memory_file)}),
                    },
                }],
            },
            {'role': 'tool', 'tool_call_id': 'c1', 'content': '# notes'},
        ],
        tool_calls=[],
    )
    monkeypatch.setattr('api.session_manifest._load_display_messages', lambda s: list(s.messages))

    manifest = build_session_manifest(session)
    assert manifest['references'] == []


def test_skill_view_becomes_skill_reference(tmp_path, monkeypatch):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    skills_dir = tmp_path / 'profile-home' / 'skills'
    _write_local_skill(skills_dir, 'my-skill')
    events = [
        ToolEvent(
            name='skill_view',
            args={'name': 'my-skill'},
            assistant_msg_idx=1,
            status='completed',
            result=json.dumps({
                'success': True,
                'name': 'my-skill',
                'path': str(skills_dir / 'my-skill' / 'SKILL.md'),
            }),
        ),
    ]
    monkeypatch.setattr('api.session_manifest._skillhub_preview_available', lambda: True)

    _artifacts, references = _extract_artifacts_and_references(events, workspace, skills_dir=skills_dir)
    wire = _rows_to_wire(references, workspace, skills_dir, collection='references')

    assert wire == [{
        'path': 'my-skill',
        'preview': MANIFEST_PREVIEW_SKILL,
        'source_tool': 'skill_view',
    }]


def test_skill_view_reference_missing_marked_expired(tmp_path, monkeypatch):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    skills_dir = tmp_path / 'profile-home' / 'skills'
    skills_dir.mkdir(parents=True)
    events = [
        ToolEvent(
            name='skill_view',
            args={'name': 'missing-skill'},
            assistant_msg_idx=1,
            status='completed',
            result=json.dumps({
                'success': True,
                'name': 'missing-skill',
            }),
        ),
    ]
    monkeypatch.setattr('api.session_manifest._skillhub_preview_available', lambda: True)

    _artifacts, references = _extract_artifacts_and_references(events, workspace, skills_dir=skills_dir)
    wire = _rows_to_wire(references, workspace, skills_dir, collection='references')

    assert wire == [{
        'path': 'missing-skill',
        'preview': MANIFEST_PREVIEW_SKILL,
        'source_tool': 'skill_view',
        'status': 'expired',
    }]


def test_build_session_manifest_skips_failed_ambiguous_skill_view_reference(tmp_path, monkeypatch):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    skills_dir = tmp_path / 'profile-home' / 'skills'
    _write_local_skill(skills_dir, 'knowledge-base-service', rel_path='ai-与机器学习/knowledge-base-service')
    _write_local_skill(skills_dir, 'knowledge-base-service', rel_path='ops/knowledge-base-service')
    session = Session(
        session_id='manifestambiguousskill01',
        workspace=str(workspace),
        profile='default',
        messages=[
            {'role': 'user', 'content': 'inspect kb skill', '_turn_key': 'turn:1'},
            {
                'role': 'assistant',
                'tool_calls': [
                    {
                        'id': 'c1',
                        'function': {
                            'name': 'skill_view',
                            'arguments': json.dumps({'name': 'knowledge-base-service'}),
                        },
                    },
                    {
                        'id': 'c2',
                        'function': {
                            'name': 'skill_view',
                            'arguments': json.dumps({'name': 'ai-与机器学习/knowledge-base-service'}),
                        },
                    },
                ],
            },
            {
                'role': 'tool',
                'tool_call_id': 'c1',
                'content': json.dumps({
                    'success': False,
                    'error': "Ambiguous skill name 'knowledge-base-service': 2 skills match",
                }),
            },
            {
                'role': 'tool',
                'tool_call_id': 'c2',
                'content': json.dumps({
                    'success': True,
                    'name': 'knowledge-base-service',
                    'path': str(skills_dir / 'ai-与机器学习' / 'knowledge-base-service' / 'SKILL.md'),
                }),
            },
        ],
        tool_calls=[],
    )
    monkeypatch.setattr('api.session_manifest._load_display_messages', lambda s: list(s.messages))
    monkeypatch.setattr('api.session_manifest._skills_dir_for_session', lambda s: skills_dir)
    monkeypatch.setattr('api.session_manifest._skillhub_preview_available', lambda: True)

    manifest = build_session_manifest(session)

    assert manifest['references'] == [{
        'path': 'ai-与机器学习/knowledge-base-service',
        'preview': MANIFEST_PREVIEW_SKILL,
        'source_tool': 'skill_view',
    }]
    assert manifest['turns'][0]['references'] == manifest['references']


def _write_local_skill(
    skills_dir: Path,
    name: str,
    body: str = '# Skill',
    *,
    rel_path: str | None = None,
) -> None:
    rel = rel_path or name
    skill_dir = skills_dir / rel
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / 'SKILL.md').write_text(
        f'---\nname: {name}\ndescription: test\n---\n\n{body}',
        encoding='utf-8',
    )


def test_skill_manage_create_becomes_skill_artifact(tmp_path, monkeypatch):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    skills_dir = tmp_path / 'profile-home' / 'skills'
    _write_local_skill(skills_dir, 'foo')
    events = [
        ToolEvent(
            name='skill_manage',
            args={'action': 'create', 'name': 'foo', 'content': '# Skill'},
            assistant_msg_idx=1,
            status='completed',
            result='{"success": true, "path": "foo"}',
        ),
    ]
    monkeypatch.setattr('api.session_manifest._skillhub_preview_available', lambda: True)

    artifacts, references = _extract_artifacts_and_references(events, workspace, skills_dir=skills_dir)
    wire = _rows_to_wire(artifacts, workspace, skills_dir, collection='artifacts')

    assert references == []
    assert wire == [{
        'path': 'foo',
        'preview': MANIFEST_PREVIEW_SKILL,
        'source_tool': 'skill_manage',
    }]


def test_skill_manage_artifact_deleted_marked_expired(tmp_path, monkeypatch):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    skills_dir = tmp_path / 'profile-home' / 'skills'
    _write_local_skill(skills_dir, 'gone-skill')
    events = [
        ToolEvent(
            name='skill_manage',
            args={'action': 'create', 'name': 'gone-skill', 'content': '# Skill'},
            assistant_msg_idx=1,
            status='completed',
            result='{"success": true, "path": "gone-skill"}',
        ),
    ]
    monkeypatch.setattr('api.session_manifest._skillhub_preview_available', lambda: True)

    artifacts, _references = _extract_artifacts_and_references(events, workspace, skills_dir=skills_dir)
    shutil = __import__('shutil')
    shutil.rmtree(skills_dir / 'gone-skill')
    wire = _rows_to_wire(artifacts, workspace, skills_dir, collection='artifacts')

    assert wire == [{
        'path': 'gone-skill',
        'preview': MANIFEST_PREVIEW_SKILL,
        'source_tool': 'skill_manage',
        'status': 'expired',
    }]


def test_extract_manifest_delta_read_file_has_no_public_reference(tmp_path, monkeypatch):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    event = ToolEvent(
        name='read_file',
        args={'path': 'missing.txt'},
        assistant_msg_idx=1,
        status='completed',
    )
    delta = extract_manifest_delta_from_tool_event(
        event,
        workspace,
        session_id='sess01',
        stream_id='stream01',
        turn_key='turn:0',
        sequence=1,
    )
    assert delta['references'] == []
    assert delta['artifacts'] == []


def test_read_evidence_suppresses_only_final_assistant_prose(tmp_path, monkeypatch):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    target = workspace / 'input.md'
    target.write_text('input', encoding='utf-8')
    session = Session(
        session_id='read_evidence_prose01',
        workspace=str(workspace),
        messages=[
            {'role': 'user', 'content': 'inspect', '_turn_key': 'turn:0'},
            {
                'role': 'assistant',
                'tool_calls': [{
                    'id': 'read-1',
                    'function': {'name': 'read_file', 'arguments': json.dumps({'path': 'input.md'})},
                }],
            },
            {'role': 'tool', 'tool_call_id': 'read-1', 'name': 'read_file', 'content': 'input'},
            {'role': 'assistant', 'content': 'See `input.md`.'},
        ],
        tool_calls=[],
    )
    monkeypatch.setattr('api.session_manifest._load_display_messages', lambda s: list(s.messages))
    monkeypatch.setattr('api.session_manifest_store.load_manifest_records', lambda *a, **k: [])
    monkeypatch.setattr('api.session_manifest_store.load_manifest_decided_turn_keys', lambda *a, **k: set())
    monkeypatch.setattr('api.session_manifest_store.repair_empty_manifest_turns', lambda *a, **k: 0)
    monkeypatch.setattr('api.session_manifest_store.backfill_missing_manifest_records', lambda *a, **k: {'source': 'derived'})

    manifest = build_session_manifest(session)

    assert manifest['artifacts'] == []
    assert manifest['references'] == []


def test_read_then_edit_same_path_keeps_mutation_artifact(tmp_path, monkeypatch):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    (workspace / 'report.md').write_text('updated', encoding='utf-8')
    session = Session(
        session_id='read_edit_artifact01',
        workspace=str(workspace),
        messages=[
            {'role': 'user', 'content': 'update', '_turn_key': 'turn:0'},
            {
                'role': 'assistant',
                'tool_calls': [{
                    'id': 'read-1',
                    'function': {'name': 'read_file', 'arguments': json.dumps({'path': 'report.md'})},
                }],
            },
            {'role': 'tool', 'tool_call_id': 'read-1', 'name': 'read_file', 'content': 'old'},
            {
                'role': 'assistant',
                'tool_calls': [{
                    'id': 'edit-1',
                    'function': {'name': 'edit_file', 'arguments': json.dumps({'path': 'report.md'})},
                }],
            },
            {'role': 'tool', 'tool_call_id': 'edit-1', 'name': 'edit_file', 'content': 'updated'},
            {'role': 'assistant', 'content': 'Updated `report.md`.'},
        ],
        tool_calls=[],
    )
    monkeypatch.setattr('api.session_manifest._load_display_messages', lambda s: list(s.messages))
    monkeypatch.setattr('api.session_manifest_store.load_manifest_records', lambda *a, **k: [])
    monkeypatch.setattr('api.session_manifest_store.load_manifest_decided_turn_keys', lambda *a, **k: set())
    monkeypatch.setattr('api.session_manifest_store.repair_empty_manifest_turns', lambda *a, **k: 0)
    monkeypatch.setattr('api.session_manifest_store.backfill_missing_manifest_records', lambda *a, **k: {'source': 'derived'})

    manifest = build_session_manifest(session)

    assert manifest['references'] == []
    assert manifest['artifacts'] == [{
        'path': 'report.md',
        'preview': MANIFEST_PREVIEW_FILE,
        'source_tool': 'edit_file',
    }]


def test_extract_manifest_delta_skill_reference_expired(tmp_path, monkeypatch):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    skills_dir = tmp_path / 'profile-home' / 'skills'
    skills_dir.mkdir(parents=True)
    monkeypatch.setattr('api.session_manifest._skillhub_preview_available', lambda: True)
    event = ToolEvent(
        name='skill_view',
        args={'name': 'missing-skill'},
        assistant_msg_idx=1,
        status='completed',
        result=json.dumps({'success': True, 'name': 'missing-skill'}),
    )
    delta = extract_manifest_delta_from_tool_event(
        event,
        workspace,
        session_id='sess01',
        stream_id='stream01',
        turn_key='turn:0',
        sequence=1,
        skills_dir=skills_dir,
    )
    assert delta['references'] == [{
        'path': 'missing-skill',
        'preview': MANIFEST_PREVIEW_SKILL,
        'source_tool': 'skill_view',
        'status': 'expired',
    }]


def test_build_session_manifest_prefers_store_skill_artifact(tmp_path, monkeypatch):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    skills_dir = tmp_path / 'profile-home' / 'skills'
    _write_local_skill(skills_dir, 'stored-skill')
    session = Session(
        session_id='manifestskillstore01',
        workspace=str(workspace),
        profile='ops',
        messages=[
            {'role': 'user', 'content': 'create skill', '_turn_key': 'turn:1'},
        ],
        tool_calls=[],
    )
    monkeypatch.setattr('api.session_manifest._load_display_messages', lambda s: list(s.messages))
    monkeypatch.setattr('api.session_manifest._skillhub_preview_available', lambda: True)
    monkeypatch.setattr(
        'api.session_manifest._skills_dir_for_session',
        lambda s: skills_dir,
    )
    monkeypatch.setattr(
        'api.session_manifest_store.load_manifest_records',
        lambda s, include_lineage=True: [{
            'session_id': s.session_id,
            'lineage_key': s.session_id,
            'profile': 'ops',
            'turn_key': 'turn:1',
            'record_kind': 'artifact',
            'path': 'stored-skill',
            'preview': 'skill',
            'source_tool': 'skill_manage',
        }],
    )

    manifest = build_session_manifest(session)
    assert manifest['artifacts'] == [{
        'path': 'stored-skill',
        'preview': MANIFEST_PREVIEW_SKILL,
        'source_tool': 'skill_manage',
        'profile': 'ops',
    }]

    shutil = __import__('shutil')
    shutil.rmtree(skills_dir / 'stored-skill')
    manifest = build_session_manifest(session)
    assert manifest['artifacts'] == [{
        'path': 'stored-skill',
        'preview': MANIFEST_PREVIEW_SKILL,
        'source_tool': 'skill_manage',
        'profile': 'ops',
        'status': 'expired',
    }]


def test_skill_manage_patch_becomes_skill_artifact(tmp_path, monkeypatch):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    skills_dir = tmp_path / 'profile-home' / 'skills'
    _write_local_skill(skills_dir, 'bar')
    events = [
        ToolEvent(
            name='skill_manage',
            args={'action': 'patch', 'name': 'bar', 'old_string': 'a', 'new_string': 'b'},
            assistant_msg_idx=1,
            status='completed',
        ),
    ]
    monkeypatch.setattr('api.session_manifest._skillhub_preview_available', lambda: True)

    artifacts, _references = _extract_artifacts_and_references(events, workspace, skills_dir=skills_dir)
    wire = _rows_to_wire(artifacts, workspace, skills_dir)

    assert wire == [{
        'path': 'bar',
        'preview': MANIFEST_PREVIEW_SKILL,
        'source_tool': 'skill_manage',
    }]


def test_skill_manage_create_uses_result_path_for_category(tmp_path, monkeypatch):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    skills_dir = tmp_path / 'profile-home' / 'skills'
    _write_local_skill(skills_dir, 'github-trending', rel_path='github/github-trending')
    result = json.dumps({
        'success': True,
        'path': 'github/github-trending',
        'skill_md': str(skills_dir / 'github/github-trending/SKILL.md'),
    })
    events = [
        ToolEvent(
            name='skill_manage',
            args={
                'action': 'create',
                'name': 'github-trending',
                'category': 'github',
                'content': '# Skill',
            },
            assistant_msg_idx=1,
            status='completed',
            result=result,
        ),
    ]
    monkeypatch.setattr('api.session_manifest._skillhub_preview_available', lambda: True)

    artifacts, _references = _extract_artifacts_and_references(events, workspace, skills_dir=skills_dir)
    wire = _rows_to_wire(artifacts, workspace, skills_dir)

    assert wire == [{
        'path': 'github/github-trending',
        'preview': MANIFEST_PREVIEW_SKILL,
        'source_tool': 'skill_manage',
    }]


def test_skill_manage_create_canonicalizes_absolute_result_path(tmp_path, monkeypatch):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    skills_dir = tmp_path / 'profile-home' / 'skills'
    skill_rel = '数据分析/data-analysis'
    _write_local_skill(skills_dir, 'data-analysis', rel_path=skill_rel)
    result = json.dumps({
        'success': True,
        'path': str(skills_dir / skill_rel),
    })
    events = [
        ToolEvent(
            name='skill_manage',
            args={'action': 'edit', 'name': 'data-analysis', 'content': '# Skill'},
            assistant_msg_idx=1,
            status='completed',
            result=result,
        ),
    ]
    monkeypatch.setattr('api.session_manifest._skillhub_preview_available', lambda: True)

    artifacts, _references = _extract_artifacts_and_references(events, workspace, skills_dir=skills_dir)
    wire = _rows_to_wire(artifacts, workspace, skills_dir)

    assert wire == [{
        'path': skill_rel,
        'preview': MANIFEST_PREVIEW_SKILL,
        'source_tool': 'skill_manage',
    }]


def test_build_session_manifest_dedupes_legacy_stripped_skill_store_path(tmp_path, monkeypatch):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    skills_dir = tmp_path / 'profile-home' / 'skills'
    skill_rel = '数据分析/data-analysis'
    _write_local_skill(skills_dir, 'data-analysis', rel_path=skill_rel)
    skill_md = skills_dir / skill_rel / 'SKILL.md'
    legacy_path = str(skills_dir / skill_rel).lstrip('/')
    session = Session(
        session_id='manifestlegacyabspath01',
        workspace=str(workspace),
        profile='test-profile',
        messages=[
            {'role': 'user', 'content': 'patch skill', '_turn_key': 'turn:1'},
            {
                'role': 'assistant',
                'tool_calls': [{
                    'id': 'c1',
                    'function': {
                        'name': 'patch',
                        'arguments': json.dumps({'path': str(skill_md)}),
                    },
                }],
            },
            {'role': 'tool', 'tool_call_id': 'c1', 'content': 'updated'},
        ],
        tool_calls=[],
    )
    monkeypatch.setattr('api.session_manifest._load_display_messages', lambda s: list(s.messages))
    monkeypatch.setattr('api.session_manifest._skills_dir_for_session', lambda s: skills_dir)
    monkeypatch.setattr('api.session_manifest._skillhub_preview_available', lambda: True)
    monkeypatch.setattr(
        'api.session_manifest_store.load_manifest_records',
        lambda s, include_lineage=True: [{
            'session_id': s.session_id,
            'lineage_key': s.session_id,
            'profile': 'test-profile',
            'turn_key': 'turn:2',
            'record_kind': 'artifact',
            'path': legacy_path,
            'preview': 'skill',
            'source_tool': 'skill_manage',
        }],
    )

    manifest = build_session_manifest(session)

    assert manifest['artifacts'] == [{
        'path': skill_rel,
        'preview': MANIFEST_PREVIEW_SKILL,
        'source_tool': 'skill_manage',
        'profile': 'test-profile',
    }]
    assert all(row.get('status') != 'expired' for row in manifest['artifacts'])
    assert all(not turn['artifacts'] for turn in manifest['turns'])
    assert manifest['diagnostics']['orphan_turn_keys'] == ['turn:2']


def test_skill_manage_delete_is_not_artifact(tmp_path, monkeypatch):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    skills_dir = tmp_path / 'profile-home' / 'skills'
    events = [
        ToolEvent(
            name='skill_manage',
            args={'action': 'delete', 'name': 'gone'},
            assistant_msg_idx=1,
            status='completed',
        ),
    ]
    monkeypatch.setattr('api.session_manifest._skillhub_preview_available', lambda: True)

    artifacts, references = _extract_artifacts_and_references(events, workspace, skills_dir=skills_dir)
    assert artifacts == []
    assert references == []


def test_skill_mutation_without_name_is_omitted(tmp_path, monkeypatch):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    skills_dir = tmp_path / 'profile-home' / 'skills'
    skills_dir.mkdir(parents=True)
    events = [ToolEvent(name='skill_manage', args={'action': 'create', 'content': '# no name'}, assistant_msg_idx=1)]
    monkeypatch.setattr('api.session_manifest._skillhub_preview_available', lambda: True)

    artifacts, references = _extract_artifacts_and_references(events, workspace, skills_dir=skills_dir)
    assert _rows_to_wire(artifacts, workspace, skills_dir) == []
    assert references == []


def test_skill_mutation_in_progress_is_not_wired(tmp_path, monkeypatch):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    skills_dir = tmp_path / 'profile-home' / 'skills'
    _write_local_skill(skills_dir, 'pending-skill')
    events = [
        ToolEvent(
            name='skill_manage',
            args={'action': 'create', 'name': 'pending-skill', 'content': '# Skill'},
            assistant_msg_idx=1,
            status='in_progress',
        ),
    ]
    monkeypatch.setattr('api.session_manifest._skillhub_preview_available', lambda: True)

    artifacts, _references = _extract_artifacts_and_references(events, workspace, skills_dir=skills_dir)
    assert _rows_to_wire(artifacts, workspace, skills_dir) == []


def test_skill_mutation_omitted_when_integration_disabled(tmp_path, monkeypatch):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    skills_dir = tmp_path / 'profile-home' / 'skills'
    _write_local_skill(skills_dir, 'foo')
    events = [
        ToolEvent(
            name='skill_manage',
            args={'action': 'create', 'name': 'foo', 'content': '# Skill'},
            assistant_msg_idx=1,
            status='completed',
        ),
    ]
    monkeypatch.setattr('api.session_manifest._skillhub_preview_available', lambda: False)

    artifacts, _references = _extract_artifacts_and_references(events, workspace, skills_dir=skills_dir)
    assert _rows_to_wire(artifacts, workspace, skills_dir) == []


def test_write_file_skill_md_becomes_skill_artifact(tmp_path, monkeypatch):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    skills_dir = tmp_path / 'profile-home' / 'skills'
    skill_md = skills_dir / 'my-skill' / 'SKILL.md'
    skill_md.parent.mkdir(parents=True)
    skill_md.write_text('# My Skill', encoding='utf-8')
    events = [
        ToolEvent(
            name='write_file',
            args={'path': str(skill_md)},
            assistant_msg_idx=1,
            status='completed',
        ),
    ]
    monkeypatch.setattr('api.session_manifest._skillhub_preview_available', lambda: True)

    artifacts, references = _extract_artifacts_and_references(events, workspace, skills_dir=skills_dir)
    wire = _rows_to_wire(artifacts, workspace, skills_dir)

    assert references == []
    assert wire == [{
        'path': 'my-skill',
        'preview': MANIFEST_PREVIEW_SKILL,
        'source_tool': 'write_file',
    }]


def test_write_file_category_skill_md_becomes_skill_artifact(tmp_path, monkeypatch):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    skills_dir = tmp_path / 'profile-home' / 'skills'
    skill_md = skills_dir / 'github' / 'github-trending' / 'SKILL.md'
    skill_md.parent.mkdir(parents=True)
    skill_md.write_text('# Trending', encoding='utf-8')
    events = [
        ToolEvent(
            name='write_file',
            args={'path': str(skill_md)},
            assistant_msg_idx=1,
            status='completed',
        ),
    ]
    monkeypatch.setattr('api.session_manifest._skillhub_preview_available', lambda: True)

    artifacts, _references = _extract_artifacts_and_references(events, workspace, skills_dir=skills_dir)
    wire = _rows_to_wire(artifacts, workspace, skills_dir)

    assert wire == [{
        'path': 'github/github-trending',
        'preview': MANIFEST_PREVIEW_SKILL,
        'source_tool': 'write_file',
    }]


def test_write_file_non_skill_md_under_skills_is_not_skill_artifact(tmp_path, monkeypatch):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    skills_dir = tmp_path / 'profile-home' / 'skills'
    readme = skills_dir / 'my-skill' / 'README.md'
    readme.parent.mkdir(parents=True)
    readme.write_text('# readme', encoding='utf-8')
    events = [
        ToolEvent(
            name='write_file',
            args={'path': str(readme)},
            assistant_msg_idx=1,
            status='completed',
        ),
    ]
    monkeypatch.setattr('api.session_manifest._skillhub_preview_available', lambda: True)

    artifacts, _references = _extract_artifacts_and_references(events, workspace, skills_dir=skills_dir)
    assert _rows_to_wire(artifacts, workspace, skills_dir) == []


def test_write_file_skill_md_in_progress_is_not_wired(tmp_path, monkeypatch):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    skills_dir = tmp_path / 'profile-home' / 'skills'
    skill_md = skills_dir / 'pending-skill' / 'SKILL.md'
    skill_md.parent.mkdir(parents=True)
    skill_md.write_text('# pending', encoding='utf-8')
    events = [
        ToolEvent(
            name='write_file',
            args={'path': str(skill_md)},
            assistant_msg_idx=1,
            status='in_progress',
        ),
    ]
    monkeypatch.setattr('api.session_manifest._skillhub_preview_available', lambda: True)

    artifacts, _references = _extract_artifacts_and_references(events, workspace, skills_dir=skills_dir)
    assert _rows_to_wire(artifacts, workspace, skills_dir) == []


def test_write_file_and_skill_manage_same_skill_dedupes(tmp_path, monkeypatch):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    skills_dir = tmp_path / 'profile-home' / 'skills'
    _write_local_skill(skills_dir, 'dup-skill')
    skill_md = skills_dir / 'dup-skill' / 'SKILL.md'
    events = [
        ToolEvent(
            name='skill_manage',
            args={'action': 'create', 'name': 'dup-skill', 'content': '# Skill'},
            assistant_msg_idx=1,
            status='completed',
            result='{"success": true, "path": "dup-skill"}',
        ),
        ToolEvent(
            name='write_file',
            args={'path': str(skill_md)},
            assistant_msg_idx=1,
            status='completed',
        ),
    ]
    monkeypatch.setattr('api.session_manifest._skillhub_preview_available', lambda: True)

    artifacts, _references = _extract_artifacts_and_references(events, workspace, skills_dir=skills_dir)
    wire = _rows_to_wire(artifacts, workspace, skills_dir)

    assert len(wire) == 1
    assert wire[0]['path'] == 'dup-skill'
    assert wire[0]['preview'] == MANIFEST_PREVIEW_SKILL


def test_build_session_manifest_skill_manage_in_turn_artifacts(tmp_path, monkeypatch):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    skills_dir = tmp_path / 'profile-home' / 'skills'
    _write_local_skill(skills_dir, 'turn-skill')
    session = Session(
        session_id='manifestskill01',
        workspace=str(workspace),
        profile='test-profile',
        messages=[
            {'role': 'user', 'content': 'create skill'},
            {
                'role': 'assistant',
                'tool_calls': [{
                    'id': 'c1',
                    'function': {
                        'name': 'skill_manage',
                        'arguments': json.dumps({
                            'action': 'create',
                            'name': 'turn-skill',
                            'content': '# Skill',
                        }),
                    },
                }],
            },
            {
                'role': 'tool',
                'tool_call_id': 'c1',
                'content': json.dumps({'success': True, 'path': 'turn-skill'}),
            },
        ],
        tool_calls=[],
    )
    monkeypatch.setattr('api.session_manifest._load_display_messages', lambda s: list(s.messages))
    monkeypatch.setattr('api.session_manifest._skills_dir_for_session', lambda s: skills_dir)
    monkeypatch.setattr('api.session_manifest._skillhub_preview_available', lambda: True)

    manifest = build_session_manifest(session)
    assert manifest['artifacts'] == [{
        'path': 'turn-skill',
        'preview': MANIFEST_PREVIEW_SKILL,
        'source_tool': 'skill_manage',
        'profile': 'test-profile',
    }]
    assert manifest['references'] == []
    assert manifest['turns'][0]['artifacts'] == manifest['artifacts']


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
    assert 'session_id' not in manifest
    assert 'counts' not in manifest
    assert manifest['todos'] == {'items': []}
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

    assert len(manifest['turns']) == 2
    first_turn, second_turn = manifest['turns']
    assert first_turn['turn_key'] == 'turn:0'
    assert first_turn['artifacts'] == [{
        'path': 'first.txt',
        'preview': 'file',
        'source_tool': 'write_file',
    }]
    assert second_turn['turn_key'] == 'turn:3'
    assert second_turn['artifacts'] == [{
        'path': 'second.txt',
        'preview': 'file',
        'source_tool': 'write_file',
    }]
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
        todo_event, workspace, session_id='sid', stream_id='stream1', turn_key='turn:0', sequence=1,
    )
    write_delta = extract_manifest_delta_from_tool_event(
        write_event, workspace, session_id='sid', stream_id='stream1', turn_key='turn:0', sequence=2,
    )

    assert todo_delta['todos']['items'] == [{'id': 't1', 'content': 'Ship', 'status': 'unknown'}]
    assert write_delta['artifacts'] == []


def test_merge_manifest_delta_is_idempotent_by_path():
    base = {'todos': {'items': []}, 'artifacts': [], 'references': [], 'turns': []}
    delta = {
        'session_id': 'sid',
        'stream_id': 'stream1',
        'turn_key': 'turn:0',
        'sequence': 1,
        'artifacts': [{
            'path': 'notes.txt',
            'preview': 'file',
            'source_tool': 'write_file',
        }],
        'references': [],
    }

    merged = merge_manifest_delta(merge_manifest_delta(base, delta), delta)

    assert merged['artifacts'] == [{
        'path': 'notes.txt',
        'preview': 'file',
        'source_tool': 'write_file',
    }]
    assert len(merged['turns']) == 1
    assert merged['turns'][0]['artifacts'] == [{
        'path': 'notes.txt',
        'preview': 'file',
        'source_tool': 'write_file',
    }]


def test_merge_manifest_delta_merges_partial_todo_updates_by_id():
    base = {
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


def test_extract_latest_todos_scoped_to_current_turn():
    messages = [
        {'role': 'user', 'content': 'first'},
        {
            'role': 'tool',
            'content': json.dumps({
                'todos': [{'id': '1', 'content': 'Old task', 'status': 'completed'}],
            }),
        },
        {'role': 'user', 'content': 'second'},
        {'role': 'assistant', 'content': 'ok'},
    ]
    latest = _extract_latest_todos(messages)
    assert latest['items'] == []


def test_build_session_manifest_omits_previous_turn_todos(tmp_path, monkeypatch):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    session = Session(
        session_id='manifesttodos02',
        workspace=str(workspace),
        messages=[
            {'role': 'user', 'content': 'first'},
            {
                'role': 'tool',
                'content': json.dumps({
                    'todos': [{'id': '1', 'content': 'Only turn one', 'status': 'pending'}],
                }),
            },
            {'role': 'user', 'content': 'second'},
            {'role': 'assistant', 'content': 'done'},
        ],
        tool_calls=[],
    )
    monkeypatch.setattr('api.session_manifest._load_display_messages', lambda s: list(s.messages))

    manifest = build_session_manifest(session)

    assert manifest['todos']['items'] == []


def test_public_todo_items_filters_id_only_and_no_description():
    assert _public_todo_items([
        {'id': '1', 'status': 'pending'},
        {'id': '2', 'content': '(no description)', 'status': 'pending'},
        {'id': '3', 'content': 'Ship it', 'status': 'in_progress'},
    ]) == [{'id': '3', 'content': 'Ship it', 'status': 'in_progress'}]


def test_wire_todos_uses_public_items_only():
    wired = _wire_todos({
        'items': [
            {'id': '1', 'content': '', 'status': 'pending'},
            {'id': '2', 'content': 'Visible', 'status': 'pending'},
        ],
    })
    assert wired == {'items': [{'id': '2', 'content': 'Visible', 'status': 'pending'}]}


def test_apply_public_todos_to_manifest_delta_drops_id_only_after_merge():
    base = {'todos': {'items': []}, 'artifacts': [], 'references': [], 'turns': []}
    delta = {
        'todos': {
            'items': [{'id': '1', 'status': 'pending'}],
            'mode': 'replace_latest',
        },
    }
    live = merge_manifest_delta(base, delta)
    _apply_public_todos_to_manifest_delta(delta, live)
    assert 'todos' not in delta
    assert live['todos']['items'] == [{'id': '1', 'content': '', 'status': 'pending'}]


def test_apply_public_todos_to_manifest_delta_emits_after_content_merge():
    base = {
        'todos': {
            'items': [
                {'id': '1', 'content': 'Plan', 'status': 'in_progress'},
            ],
        },
        'artifacts': [],
        'references': [],
        'turns': [],
    }
    delta = {
        'todos': {
            'items': [{'id': '2', 'status': 'pending'}],
            'mode': 'replace_latest',
        },
    }
    live = merge_manifest_delta(base, delta)
    live = merge_manifest_delta(live, {
        'todos': {
            'items': [{'id': '2', 'content': 'Research', 'status': 'pending'}],
            'mode': 'replace_latest',
        },
    })
    outbound = {
        'todos': {
            'items': [{'id': '2', 'content': 'Research', 'status': 'pending'}],
            'mode': 'replace_latest',
        },
    }
    _apply_public_todos_to_manifest_delta(outbound, live)
    assert outbound['todos']['items'] == [
        {'id': '1', 'content': 'Plan', 'status': 'in_progress'},
        {'id': '2', 'content': 'Research', 'status': 'pending'},
    ]


def test_paths_from_assistant_media_skips_remote_urls(tmp_path):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    local_file = tmp_path / 'local.png'
    local_file.write_bytes(b'png')
    text = f'Here MEDIA:https://cdn.example.com/img.png and MEDIA:{local_file}'
    paths = _paths_from_assistant_media(text, workspace)
    assert paths == [local_file.resolve().as_posix()]


def test_paths_from_last_assistant_message_accepts_parenthesized_filename(tmp_path):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    artifact = workspace / '中国共产党纪律处分条例（2018年版）.docx'
    artifact.write_text('docx-like', encoding='utf-8')
    text = f'Done.\nMEDIA:{artifact.as_posix()}'
    paths = _paths_from_last_assistant_message(text, workspace)
    assert paths == ['中国共产党纪律处分条例（2018年版）.docx']


def test_paths_from_last_assistant_message_accepts_ascii_parentheses_filename(tmp_path):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    artifact = workspace / 'report(2018).docx'
    artifact.write_text('docx-like', encoding='utf-8')
    text = f'Done.\nMEDIA:{artifact.as_posix()}'
    paths = _paths_from_last_assistant_message(text, workspace)
    assert paths == ['report(2018).docx']


def test_paths_from_last_assistant_message_accepts_tilde_workspace_path(tmp_path, monkeypatch):
    fake_home = tmp_path / 'home'
    workspace = fake_home / 'workspace'
    workspace.mkdir(parents=True)
    artifact = workspace / 'OpenAI最新模型定价.docx'
    artifact.write_text('docx-like', encoding='utf-8')
    monkeypatch.setenv('HOME', str(fake_home))
    text = '文件位置：~/workspace/OpenAI最新模型定价.docx'
    paths = _paths_from_last_assistant_message(text, workspace)
    assert paths == ['OpenAI最新模型定价.docx']


def test_paths_from_last_assistant_message_scans_final_assistant_text(tmp_path):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    (workspace / '1.docx').write_text('docx-like', encoding='utf-8')
    (workspace / 'report.docx').write_text('docx-like', encoding='utf-8')

    text = 'I checked 1.docx and 已生成：report.docx.'
    assert _paths_from_last_assistant_message(text, workspace) == ['1.docx', 'report.docx']


def test_paths_from_last_assistant_message_scans_file_label_without_delivery_regex(tmp_path):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    (workspace / '2026世界杯_7月10日今日战况.docx').write_text('docx-like', encoding='utf-8')

    text = 'Word 文档已生成！📄\n\n文件： 2026世界杯_7月10日今日战况.docx（38KB）'

    assert _paths_from_last_assistant_message(text, workspace) == ['2026世界杯_7月10日今日战况.docx']


def test_paths_from_last_assistant_message_normalizes_duplicate_workspace_prefix(tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    artifact = workspace / '晚间新闻简报_20260726.pdf'
    artifact.write_bytes(b'%PDF-1.4')

    text = '已生成：`workspace/晚间新闻简报_20260726.pdf`'

    assert _paths_from_last_assistant_message(text, workspace) == ['晚间新闻简报_20260726.pdf']


def test_build_session_manifest_does_not_promote_skill_or_terminal_tool_result_text(tmp_path, monkeypatch):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    (workspace / 'SKILL.md').write_text('# skill mention', encoding='utf-8')
    (workspace / '1.docx').write_text('docx-like', encoding='utf-8')
    skills_dir = tmp_path / 'skills'
    _write_local_skill(skills_dir, 'diagnose')
    session = Session(
        session_id='manifestfalseartifact01',
        workspace=str(workspace),
        profile='ops',
        messages=[
            {'role': 'user', 'content': 'inspect skill and terminal', '_turn_key': 'turn:0'},
            {
                'role': 'assistant',
                'content': [
                    {
                        'type': 'tool_use',
                        'id': 'toolu_skill',
                        'name': 'skill_view',
                        'input': {'name': 'diagnose'},
                    },
                ],
            },
            {'role': 'tool', 'tool_call_id': 'toolu_skill', 'content': json.dumps({
                'success': True,
                'name': 'diagnose',
                'path': str(skills_dir / 'diagnose' / 'SKILL.md'),
            }) + '\nRead SKILL.md and 1.docx references.'},
            {
                'role': 'assistant',
                'content': [
                    {
                        'type': 'tool_use',
                        'id': 'toolu_terminal',
                        'name': 'terminal',
                        'input': {'command': 'printf "wrote 1.docx"'},
                    },
                ],
            },
            {'role': 'tool', 'tool_call_id': 'toolu_terminal', 'content': 'wrote 1.docx'},
            {'role': 'assistant', 'content': 'I inspected the skill and terminal output.'},
        ],
        tool_calls=[],
    )
    monkeypatch.setattr('api.session_manifest._load_display_messages', lambda s: list(s.messages))
    monkeypatch.setattr('api.session_manifest._skills_dir_for_session', lambda s: skills_dir)
    monkeypatch.setattr('api.session_manifest._skillhub_preview_available', lambda: True)
    monkeypatch.setattr('api.session_manifest_store.load_manifest_records', lambda *args, **kwargs: [])
    monkeypatch.setattr('api.session_manifest_store.load_manifest_decided_turn_keys', lambda *args, **kwargs: set())

    manifest = build_session_manifest(session)

    assert manifest['artifacts'] == []
    assert manifest['references'] == [{
        'path': 'diagnose',
        'preview': MANIFEST_PREVIEW_SKILL,
        'source_tool': 'skill_view',
    }]


def test_build_session_manifest_store_empty_decision_skips_reconcile(tmp_path, monkeypatch):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    (workspace / 'report.docx').write_text('docx-like', encoding='utf-8')
    session = Session(
        session_id='manifestemptydecision01',
        workspace=str(workspace),
        profile='ops',
        messages=[
            {'role': 'user', 'content': 'make report', '_turn_key': 'turn:0'},
            {'role': 'assistant', 'content': '文件位置：report.docx'},
        ],
        tool_calls=[],
    )
    monkeypatch.setattr('api.session_manifest._load_display_messages', lambda s: list(s.messages))
    monkeypatch.setattr('api.session_manifest_store.load_manifest_records', lambda *args, **kwargs: [])
    monkeypatch.setattr('api.session_manifest_store.load_manifest_decided_turn_keys', lambda *args, **kwargs: {'turn:0'})

    manifest = build_session_manifest(session)

    assert manifest['artifacts'] == []
    assert manifest['turns'][0]['artifacts'] == []


def test_build_session_manifest_partial_store_decision_skips_whole_session_reconcile(tmp_path, monkeypatch):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    (workspace / 'stored.docx').write_text('stored', encoding='utf-8')
    (workspace / 'missing-turn.docx').write_text('new', encoding='utf-8')
    session = Session(
        session_id='manifestpartialdecision01',
        workspace=str(workspace),
        profile='ops',
        messages=[
            {'role': 'user', 'content': 'first', '_turn_key': 'turn:0'},
            {'role': 'assistant', 'content': '文件：stored.docx'},
            {'role': 'user', 'content': 'second', '_turn_key': 'turn:2'},
            {'role': 'assistant', 'content': '文件：missing-turn.docx'},
        ],
        tool_calls=[],
    )
    monkeypatch.setattr('api.session_manifest._load_display_messages', lambda s: list(s.messages))
    monkeypatch.setattr(
        'api.session_manifest_store.load_manifest_records',
        lambda *args, **kwargs: [{
            'path': 'stored.docx',
            'source_tool': 'assistant_prose',
            'preview': 'file',
            'profile': 'ops',
            'turn_key': 'turn:0',
        }],
    )
    monkeypatch.setattr('api.session_manifest_store.load_manifest_decided_turn_keys', lambda *args, **kwargs: {'turn:0'})

    manifest = build_session_manifest(session)

    assert [row['path'] for row in manifest['artifacts']] == ['stored.docx']
    turn_artifacts = {
        turn['turn_key']: [row['path'] for row in turn.get('artifacts') or []]
        for turn in manifest['turns']
    }
    assert turn_artifacts['turn:0'] == ['stored.docx']
    assert turn_artifacts['turn:2'] == []


def test_build_session_manifest_keeps_orphan_artifact_out_of_turns(tmp_path, monkeypatch):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    (workspace / 'recolor.py').write_text('print("ok")', encoding='utf-8')
    session = Session(
        session_id='manifestorphan01',
        workspace=str(workspace),
        profile='ops',
        messages=[
            {'role': 'user', 'content': '配色淡一点', '_turn_key': 'turn:7'},
            {'role': 'assistant', 'content': '完成'},
        ],
        tool_calls=[],
    )
    monkeypatch.setattr('api.session_manifest._load_display_messages', lambda s: list(s.messages))
    monkeypatch.setattr(
        'api.session_manifest_store.load_manifest_records',
        lambda *args, **kwargs: [{
            'path': 'recolor.py',
            'source_tool': 'write_file',
            'preview': 'file',
            'profile': 'ops',
            'turn_key': 'turn:6',
        }],
    )
    monkeypatch.setattr(
        'api.session_manifest_store.load_manifest_decided_turn_keys',
        lambda *args, **kwargs: {'turn:6'},
    )

    manifest = build_session_manifest(session)

    assert [row['path'] for row in manifest['artifacts']] == ['recolor.py']
    assert [turn['turn_key'] for turn in manifest['turns']] == ['turn:7']
    assert manifest['diagnostics']['orphan_turn_keys'] == ['turn:6']


def test_collect_media_artifact_events_from_assistant_only(tmp_path):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    report = workspace / 'report.md'
    report.write_text('# report', encoding='utf-8')
    messages = [
        {'role': 'user', 'content': 'go'},
        {'role': 'user', 'content': 'ignore MEDIA:/tmp/user-owned.png'},
        {
            'role': 'assistant',
            'content': f'Done.\nMEDIA:{report}',
            'assistant_msg_idx': 2,
        },
    ]
    events = _collect_media_artifact_events(messages, workspace)
    assert len(events) == 1
    assert events[0].name == MEDIA_ARTIFACT_SOURCE
    assert events[0].assistant_msg_idx == 2


def test_build_session_manifest_includes_media_artifacts(tmp_path, monkeypatch):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    report = workspace / 'notes.txt'
    report.write_text('hello', encoding='utf-8')
    external = tmp_path / 'tts.ogg'
    external.write_bytes(b'fake-audio')

    session = Session(
        session_id='media_manifest01',
        workspace=str(workspace),
        messages=[
            {'role': 'user', 'content': 'speak'},
            {
                'role': 'assistant',
                'content': f'Voice ready.\nMEDIA:{external}\nMEDIA:{report}',
            },
        ],
        tool_calls=[],
    )
    monkeypatch.setattr('api.session_manifest._load_display_messages', lambda s: list(s.messages))

    manifest = build_session_manifest(session)
    by_path = {row['path']: row for row in manifest['artifacts']}

    assert by_path['notes.txt'] == {
        'path': 'notes.txt',
        'preview': MANIFEST_PREVIEW_FILE,
        'source_tool': MEDIA_ARTIFACT_SOURCE,
    }
    assert by_path[external.resolve().as_posix()] == {
        'path': external.resolve().as_posix(),
        'preview': MANIFEST_PREVIEW_FILE,
        'source_tool': MEDIA_ARTIFACT_SOURCE,
    }
    assert manifest['turns'][0]['artifacts']


def test_media_artifact_defers_to_write_file_source(tmp_path):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    target = workspace / 'notes.txt'
    target.write_text('hello', encoding='utf-8')
    events = [
        ToolEvent(name='write_file', args={'path': 'notes.txt'}, assistant_msg_idx=1),
        ToolEvent(name=MEDIA_ARTIFACT_SOURCE, args={'path': 'notes.txt'}, assistant_msg_idx=2),
    ]
    artifacts, _references, _turns = _extract_manifest_records(events, workspace)
    assert artifacts[0]['source_tool'] == 'write_file'


def test_extract_manifest_delta_from_assistant_media_turn_scope(tmp_path):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    report = workspace / 'turn0.md'
    report.write_text('# one', encoding='utf-8')
    later = workspace / 'turn1.md'
    later.write_text('# two', encoding='utf-8')
    messages = [
        {'role': 'user', 'content': 'first'},
        {'role': 'assistant', 'content': f'MEDIA:{report}'},
        {'role': 'user', 'content': 'second'},
        {'role': 'assistant', 'content': f'MEDIA:{later}'},
    ]
    delta = extract_manifest_delta_from_assistant_media(
        messages,
        workspace,
        session_id='sid',
        stream_id='stream-1',
        turn_key='turn:0',
        sequence=3,
    )
    assert delta['source']['kind'] == 'turn_complete'
    assert delta['source']['tool'] == TURN_RECONCILE_SOURCE
    assert delta['artifacts'] == [{
        'path': 'turn0.md',
        'preview': MANIFEST_PREVIEW_FILE,
        'source_tool': MEDIA_ARTIFACT_SOURCE,
    }]
    assert delta['turns'][0]['artifacts'] == delta['artifacts']


def test_merge_manifest_delta_write_overrides_media_source(tmp_path, monkeypatch):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    (workspace / 'notes.txt').write_text('hello', encoding='utf-8')
    session = Session(
        session_id='merge_media01',
        workspace=str(workspace),
        messages=[
            {'role': 'user', 'content': 'go'},
            {'role': 'assistant', 'content': 'MEDIA:notes.txt'},
        ],
        tool_calls=[],
    )
    monkeypatch.setattr('api.session_manifest._load_display_messages', lambda s: list(s.messages))
    persisted = build_session_manifest(session)
    live_delta = extract_manifest_delta_from_tool_event(
        ToolEvent(name='write_file', args={'path': 'notes.txt'}, assistant_msg_idx=1),
        workspace,
        turn_key='turn:0',
    )
    merged = merge_manifest_delta(persisted, live_delta)
    assert merged['artifacts'][0]['source_tool'] == 'write_file'


def test_build_session_manifest_reconcile_str_replace_path(tmp_path, monkeypatch):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    target = workspace / 'src' / 'app.py'
    target.parent.mkdir()
    target.write_text('print("hi")', encoding='utf-8')
    sid = 'reconcile_str01'
    session = Session(
        session_id=sid,
        workspace=str(workspace),
        messages=[
            {'role': 'user', 'content': 'fix'},
            {
                'role': 'assistant',
                'tool_calls': [{
                    'id': 'c1',
                    'function': {
                        'name': 'str_replace',
                        'arguments': json.dumps({'path': 'src/app.py', 'old_string': 'hi', 'new_string': 'bye'}),
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
    monkeypatch.setattr('api.session_manifest._load_display_messages', lambda s: list(s.messages))
    manifest = build_session_manifest(session)
    assert manifest['artifacts'] == []


def test_build_session_manifest_reconcile_result_path_requires_existing_file(tmp_path, monkeypatch):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    existing = workspace / 'api' / 'foo.py'
    existing.parent.mkdir()
    existing.write_text('# foo', encoding='utf-8')
    sid = 'reconcile_res01'
    session = Session(
        session_id=sid,
        workspace=str(workspace),
        messages=[
            {'role': 'user', 'content': 'run'},
            {
                'role': 'assistant',
                'tool_calls': [{
                    'id': 'c1',
                    'function': {'name': 'terminal', 'arguments': '{}'},
                }],
            },
            {'role': 'tool', 'tool_call_id': 'c1', 'content': 'Wrote api/foo.py successfully'},
        ],
        tool_calls=[],
    )
    session.save()
    monkeypatch.setattr('api.models.SESSION_DIR', tmp_path)
    monkeypatch.setattr('api.models.get_state_db_session_messages', lambda *a, **k: [])
    monkeypatch.setattr('api.session_manifest._load_display_messages', lambda s: list(s.messages))
    manifest = build_session_manifest(session)
    assert manifest['artifacts'] == []


def test_build_session_manifest_reconcile_skips_missing_file(tmp_path, monkeypatch):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    sid = 'reconcile_miss01'
    session = Session(
        session_id=sid,
        workspace=str(workspace),
        messages=[
            {'role': 'user', 'content': 'run'},
            {
                'role': 'assistant',
                'tool_calls': [{
                    'id': 'c1',
                    'function': {'name': 'terminal', 'arguments': '{}'},
                }],
            },
            {'role': 'tool', 'tool_call_id': 'c1', 'content': 'Wrote missing.md successfully'},
        ],
        tool_calls=[],
    )
    session.save()
    monkeypatch.setattr('api.models.SESSION_DIR', tmp_path)
    monkeypatch.setattr('api.models.get_state_db_session_messages', lambda *a, **k: [])
    monkeypatch.setattr('api.session_manifest._load_display_messages', lambda s: list(s.messages))
    manifest = build_session_manifest(session)
    assert manifest['artifacts'] == []


def test_build_session_manifest_reconcile_skips_args_path_without_file(tmp_path, monkeypatch):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    sid = 'reconcile_nofile01'
    session = Session(
        session_id=sid,
        workspace=str(workspace),
        messages=[
            {'role': 'user', 'content': 'write'},
            {
                'role': 'assistant',
                'tool_calls': [{
                    'id': 'c1',
                    'function': {
                        'name': 'str_replace',
                        'arguments': json.dumps({'path': 'draft.txt'}),
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
    monkeypatch.setattr('api.session_manifest._load_display_messages', lambda s: list(s.messages))
    manifest = build_session_manifest(session)
    assert manifest['artifacts'] == []


def test_build_session_manifest_reconcile_dedupes_with_write_file(tmp_path, monkeypatch):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    (workspace / 'notes.txt').write_text('hello', encoding='utf-8')
    sid = 'reconcile_dedupe01'
    session = Session(
        session_id=sid,
        workspace=str(workspace),
        messages=[
            {'role': 'user', 'content': 'edit'},
            {
                'role': 'assistant',
                'tool_calls': [{
                    'id': 'c1',
                    'function': {
                        'name': 'write_file',
                        'arguments': json.dumps({'path': 'notes.txt'}),
                    },
                }],
            },
            {'role': 'tool', 'tool_call_id': 'c1', 'content': '```diff\n+++ b/notes.txt\n```'},
        ],
        tool_calls=[],
    )
    session.save()
    monkeypatch.setattr('api.models.SESSION_DIR', tmp_path)
    monkeypatch.setattr('api.models.get_state_db_session_messages', lambda *a, **k: [])
    monkeypatch.setattr('api.session_manifest._load_display_messages', lambda s: list(s.messages))
    manifest = build_session_manifest(session)
    assert len(manifest['artifacts']) == 1
    assert manifest['artifacts'][0]['source_tool'] == 'write_file'


def test_build_session_manifest_reconcile_excludes_read_file_path(tmp_path, monkeypatch):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    (workspace / 'readme.md').write_text('# hi', encoding='utf-8')
    sid = 'reconcile_read01'
    session = Session(
        session_id=sid,
        workspace=str(workspace),
        messages=[
            {'role': 'user', 'content': 'read'},
            {
                'role': 'assistant',
                'tool_calls': [{
                    'id': 'c1',
                    'function': {
                        'name': 'read_file',
                        'arguments': json.dumps({'path': 'readme.md'}),
                    },
                }],
            },
            {'role': 'tool', 'tool_call_id': 'c1', 'content': '# hi'},
        ],
        tool_calls=[],
    )
    session.save()
    monkeypatch.setattr('api.models.SESSION_DIR', tmp_path)
    monkeypatch.setattr('api.models.get_state_db_session_messages', lambda *a, **k: [])
    monkeypatch.setattr('api.session_manifest._load_display_messages', lambda s: list(s.messages))
    manifest = build_session_manifest(session)
    assert manifest['artifacts'] == []
    assert manifest['references'] == []
    assert manifest['turns'][0]['references'] == []


def test_extract_manifest_delta_from_turn_reconcile_turn_scope(tmp_path):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    turn0 = workspace / 'turn0.md'
    turn0.write_text('# one', encoding='utf-8')
    turn1 = workspace / 'turn1.md'
    turn1.write_text('# two', encoding='utf-8')
    messages = [
        {'role': 'user', 'content': 'first'},
        {
            'role': 'assistant',
            'tool_calls': [{
                'id': 'c1',
                'function': {
                    'name': 'str_replace',
                    'arguments': json.dumps({'path': 'turn0.md'}),
                },
            }],
        },
        {'role': 'tool', 'tool_call_id': 'c1', 'content': 'ok'},
        {'role': 'user', 'content': 'second'},
        {
            'role': 'assistant',
            'tool_calls': [{
                'id': 'c2',
                'function': {
                    'name': 'str_replace',
                    'arguments': json.dumps({'path': 'turn1.md'}),
                },
            }],
        },
        {'role': 'tool', 'tool_call_id': 'c2', 'content': 'ok'},
    ]
    delta = extract_manifest_delta_from_turn_reconcile(
        messages,
        workspace,
        session_id='sid',
        stream_id='stream-1',
        turn_key='turn:0',
        sequence=4,
    )
    assert delta == {}


def test_merge_manifest_delta_turn_reconcile_idempotent(tmp_path):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    (workspace / 'notes.txt').write_text('hello', encoding='utf-8')
    messages = [
        {'role': 'user', 'content': 'go'},
        {
            'role': 'assistant',
            'tool_calls': [{
                'id': 'c1',
                'function': {
                    'name': 'str_replace',
                    'arguments': json.dumps({'path': 'notes.txt'}),
                },
            }],
        },
        {'role': 'tool', 'tool_call_id': 'c1', 'content': 'ok'},
    ]
    delta = extract_manifest_delta_from_turn_reconcile(
        messages,
        workspace,
        turn_key='turn:0',
        sequence=1,
    )
    merged = merge_manifest_delta(merge_manifest_delta({'artifacts': [], 'references': [], 'turns': []}, delta), delta)
    assert merged['artifacts'] == []


def test_paths_from_last_assistant_message_accepts_unicode_filename_without_delivery_label(tmp_path):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    docx = workspace / 'AI热点top10-2026-06.docx'
    docx.write_bytes(b'fake-docx')

    paths = _paths_from_last_assistant_message(f'| `{docx.name}` | Word |', workspace)

    assert paths == ['AI热点top10-2026-06.docx']


def test_only_final_assistant_prose_contributes_artifacts(tmp_path, monkeypatch):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    intermediate = workspace / 'intermediate.md'
    final = workspace / 'final.md'
    intermediate.write_text('draft', encoding='utf-8')
    final.write_text('done', encoding='utf-8')
    session = Session(
        session_id='final_assistant_only01',
        workspace=str(workspace),
        messages=[
            {'role': 'user', 'content': 'generate', '_turn_key': 'turn:0'},
            {'role': 'assistant', 'content': f'文件路径：{intermediate}'},
            {'role': 'assistant', 'content': '| file |\n| --- |\n| `final.md` |'},
        ],
        tool_calls=[],
    )
    monkeypatch.setattr('api.session_manifest._load_display_messages', lambda s: list(s.messages))
    monkeypatch.setattr('api.session_manifest_store.load_manifest_records', lambda *a, **k: [])
    monkeypatch.setattr('api.session_manifest_store.load_manifest_decided_turn_keys', lambda *a, **k: set())
    monkeypatch.setattr('api.session_manifest_store.repair_empty_manifest_turns', lambda *a, **k: 0)
    monkeypatch.setattr('api.session_manifest_store.backfill_missing_manifest_records', lambda *a, **k: {'source': 'derived'})

    manifest = build_session_manifest(session)

    assert [row['path'] for row in manifest['artifacts']] == ['final.md']


def test_build_session_manifest_reconcile_assistant_prose_delivery(tmp_path, monkeypatch):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    docx = workspace / 'AI热点top10-2026-06.docx'
    docx.write_bytes(b'fake-docx')
    sid = 'reconcile_prose01'
    session = Session(
        session_id=sid,
        workspace=str(workspace),
        messages=[
            {'role': 'user', 'content': '生成文档'},
            {
                'role': 'assistant',
                'content': (
                    '文件已生成 ✅\n\n'
                    f'📄 文件位置：{docx}\n'
                    '📦 文件大小：38,570 字节'
                ),
            },
        ],
        tool_calls=[],
    )
    session.save()
    monkeypatch.setattr('api.models.SESSION_DIR', tmp_path)
    monkeypatch.setattr('api.models.get_state_db_session_messages', lambda *a, **k: [])
    monkeypatch.setattr('api.session_manifest._load_display_messages', lambda s: list(s.messages))
    manifest = build_session_manifest(session)
    assert manifest['artifacts'] == [{
        'path': 'AI热点top10-2026-06.docx',
        'preview': MANIFEST_PREVIEW_FILE,
        'source_tool': ASSISTANT_PROSE_ARTIFACT_SOURCE,
    }]


def test_assistant_prose_reference_paths_are_not_artifacts(tmp_path):
    workspace = tmp_path / 'ws'
    uploads = workspace / 'uploads' / '47e623586a81'
    uploads.mkdir(parents=True)
    source_docs = [
        uploads / '供应链人工智能运营工作方案_改.docx',
        uploads / '对应功能清单.docx',
    ]
    for source_doc in source_docs:
        source_doc.write_bytes(b'uploaded-reference')

    text = (
        '我先读取两份参考文档，了解现有内容后再设计方案：\n'
        f'- {source_docs[0]}\n'
        f'- {source_docs[1]}'
    )

    assert _paths_from_last_assistant_message(text, workspace) == []


def test_turn_reconcile_keeps_deliveries_but_excludes_uploaded_references(tmp_path):
    workspace = tmp_path / 'ws'
    uploads = workspace / 'uploads' / '47e623586a81'
    uploads.mkdir(parents=True)
    source_docs = [
        uploads / '供应链人工智能运营工作方案_改.docx',
        uploads / '对应功能清单.docx',
    ]
    for source_doc in source_docs:
        source_doc.write_bytes(b'uploaded-reference')
    output_html = workspace / '供应链AI运营方案框架设计.html'
    output_pdf = workspace / '供应链AI运营方案框架设计.pdf'
    output_html.write_text('<html></html>', encoding='utf-8')
    output_pdf.write_bytes(b'fake-pdf')

    messages = [
        {
            'role': 'user',
            'content': (
                '参考以上文档设计方案\n\n'
                f'[Attached files: {source_docs[0]}, {source_docs[1]}]'
            ),
        },
        {
            'role': 'assistant',
            'content': (
                '我先读取两份参考文档：\n'
                f'{source_docs[0]}\n{source_docs[1]}'
            ),
        },
        {
            'role': 'assistant',
            'content': (
                '两份文件已生成完毕：\n'
                f'MEDIA:{output_html}\n'
                f'MEDIA:{output_pdf}'
            ),
        },
    ]

    delta = extract_manifest_delta_from_turn_reconcile(
        messages,
        workspace,
        turn_key='turn:0',
        sequence=1,
    )

    assert [row['path'] for row in delta['artifacts']] == [
        '供应链AI运营方案框架设计.html',
        '供应链AI运营方案框架设计.pdf',
    ]


def test_build_session_manifest_reconcile_assistant_prose_skips_missing_file(tmp_path, monkeypatch):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    sid = 'reconcile_prose_miss01'
    session = Session(
        session_id=sid,
        workspace=str(workspace),
        messages=[
            {'role': 'user', 'content': '生成文档'},
            {
                'role': 'assistant',
                'content': '📄 文件位置：/Users/wzq/workspace/missing-热点.docx',
            },
        ],
        tool_calls=[],
    )
    session.save()
    monkeypatch.setattr('api.models.SESSION_DIR', tmp_path)
    monkeypatch.setattr('api.models.get_state_db_session_messages', lambda *a, **k: [])
    monkeypatch.setattr('api.session_manifest._load_display_messages', lambda s: list(s.messages))
    manifest = build_session_manifest(session)
    assert manifest['artifacts'] == []


def test_extract_manifest_delta_from_turn_reconcile_assistant_prose(tmp_path):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    docx = workspace / 'report.docx'
    docx.write_bytes(b'x')
    messages = [
        {'role': 'user', 'content': 'go'},
        {'role': 'assistant', 'content': f'文件位置：{docx}'},
    ]
    delta = extract_manifest_delta_from_turn_reconcile(
        messages,
        workspace,
        turn_key='turn:0',
        sequence=2,
    )
    assert delta['source']['tool'] == TURN_RECONCILE_SOURCE
    assert delta['artifacts'] == [{
        'path': 'report.docx',
        'preview': MANIFEST_PREVIEW_FILE,
        'source_tool': ASSISTANT_PROSE_ARTIFACT_SOURCE,
    }]




def test_build_session_manifest_multi_turn_assistant_prose_delivery(tmp_path, monkeypatch):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    docx = workspace / '微博热搜榜_20260612.docx'
    docx.write_bytes(b'fake-docx')
    messages = []
    for i in range(10):
        messages.extend([
            {'role': 'user', 'content': f'q{i}'},
            {'role': 'assistant', 'content': f'a{i}'},
        ])
    messages.extend([
        {'role': 'user', 'content': '给我一个word'},
        {
            'role': 'assistant',
            'tool_calls': [{
                'id': 'c1',
                'function': {'name': 'execute_code', 'arguments': json.dumps({'code': '...'})},
            }],
        },
        {
            'role': 'tool',
            'tool_call_id': 'c1',
            'name': 'execute_code',
            'content': json.dumps({
                'status': 'success',
                'output': f'✅ 已保存: {docx}\n   文件大小: 38.8 KB\n',
            }),
        },
        {
            'role': 'assistant',
            'content': f'✅ Word 文档已生成。\n\n📄 **文件路径**：`{docx}`\n',
        },
    ])
    sid = 'multi_turn_prose01'
    session = Session(
        session_id=sid,
        workspace=str(workspace),
        messages=messages,
        tool_calls=[],
    )
    session.save()
    monkeypatch.setattr('api.models.SESSION_DIR', tmp_path)
    monkeypatch.setattr('api.models.get_state_db_session_messages', lambda *a, **k: [])
    monkeypatch.setattr('api.session_manifest._load_display_messages', lambda s: list(s.messages))
    manifest = build_session_manifest(session)
    artifact_paths = {row['path'] for row in manifest['artifacts']}
    assert '微博热搜榜_20260612.docx' in artifact_paths
    sources = {row['source_tool'] for row in manifest['artifacts'] if row['path'] == '微博热搜榜_20260612.docx'}
    assert sources & {ASSISTANT_PROSE_ARTIFACT_SOURCE, 'execute_code'}


def test_build_session_manifest_turn_reconcile_scopes_session_tool_calls(tmp_path, monkeypatch):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    docx = workspace / '微博热搜榜_20260612.docx'
    docx.write_bytes(b'fake-docx')
    png = workspace / '微博热搜榜_20260612.png'
    png.write_bytes(b'fake-png')
    messages = []
    for i in range(10):
        messages.extend([
            {'role': 'user', 'content': f'q{i}'},
            {'role': 'assistant', 'content': f'a{i}'},
        ])
    messages.extend([
        {'role': 'user', 'content': '给我一个word'},
        {
            'role': 'assistant',
            'tool_calls': [{
                'id': 'c-docx',
                'function': {'name': 'execute_code', 'arguments': json.dumps({'code': '...'})},
            }],
        },
        {
            'role': 'tool',
            'tool_call_id': 'c-docx',
            'name': 'execute_code',
            'content': json.dumps({
                'status': 'success',
                'output': f'✅ 已保存: {docx}\n',
            }),
        },
        {'role': 'assistant', 'content': f'📄 **文件路径**：`{docx}`'},
        {'role': 'user', 'content': '中间轮'},
        {'role': 'assistant', 'content': '不生成文件'},
        {'role': 'user', 'content': '能给我一个图片摘要版本吗'},
        {
            'role': 'assistant',
            'tool_calls': [{
                'id': 'c-png',
                'function': {'name': 'execute_code', 'arguments': json.dumps({'code': '...'})},
            }],
        },
        {
            'role': 'tool',
            'tool_call_id': 'c-png',
            'name': 'execute_code',
            'content': json.dumps({
                'status': 'success',
                'output': f'✅ 已保存: {png}\n',
            }),
        },
        {'role': 'assistant', 'content': f'MEDIA:{png}'},
    ])
    tool_calls = [
        {
            'name': 'browser_navigate',
            'snippet': '{"success": true}',
            'assistant_msg_idx': 1,
            'tid': 'c-browser',
        },
        {
            'name': 'execute_code',
            'snippet': json.dumps({'output': f'✅ 已保存: {docx}\n'}),
            'assistant_msg_idx': 21,
            'tid': 'c-docx',
        },
        {
            'name': 'execute_code',
            'snippet': json.dumps({'output': f'✅ 已保存: {png}\n'}),
            'assistant_msg_idx': 27,
            'tid': 'c-png',
        },
    ]
    session = Session(
        session_id='turn_reconcile_scoped_tool_calls01',
        workspace=str(workspace),
        messages=messages,
        tool_calls=tool_calls,
    )
    session.save()
    monkeypatch.setattr('api.models.SESSION_DIR', tmp_path)
    monkeypatch.setattr('api.models.get_state_db_session_messages', lambda *a, **k: [])
    monkeypatch.setattr('api.session_manifest._load_display_messages', lambda s: list(s.messages))
    manifest = build_session_manifest(session)

    assert {row['path'] for row in manifest['artifacts']} == {
        '微博热搜榜_20260612.docx',
        '微博热搜榜_20260612.png',
    }
    artifacts_by_turn = {
        turn['turn_key']: [row['path'] for row in turn['artifacts']]
        for turn in manifest['turns']
    }
    assert artifacts_by_turn['turn:0'] == []
    assert artifacts_by_turn['turn:4'] == []
    assert artifacts_by_turn['turn:20'] == ['微博热搜榜_20260612.docx']
    assert artifacts_by_turn['turn:24'] == []
    assert artifacts_by_turn['turn:26'] == ['微博热搜榜_20260612.png']

    delta = extract_manifest_delta_from_turn_reconcile(
        messages,
        workspace,
        turn_key='turn:0',
        sequence=1,
        tool_calls=tool_calls,
    )
    assert delta == {}


def test_extract_turn_artifact_paths_scopes_session_tool_calls(tmp_path):
    """Earlier turns' write_file snippets must not leak into later turn extraction."""
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    report = workspace / 'report.md'
    report.write_text('# report', encoding='utf-8')

    messages = [
        {'role': 'user', 'content': 'create report', '_turn_key': 'turn:1'},
        {'role': 'assistant', 'content': 'done'},
        {'role': 'user', 'content': 'weather today', '_turn_key': 'turn:4'},
        {'role': 'assistant', 'content': 'sunny'},
    ]
    tool_calls = [
        {
            'name': 'write_file',
            'args': {'path': str(report)},
            'assistant_msg_idx': 1,
            'tid': 'write-turn1',
            'done': True,
        },
        {
            'name': 'web_search',
            'args': {'query': 'beijing weather'},
            'assistant_msg_idx': 3,
            'tid': 'search-turn4',
        },
    ]
    turns = {turn['turn_key']: turn for turn in _message_turns(messages)}

    turn1_paths = _extract_turn_artifact_paths(
        _turn_message_slice(messages, 'turn:1'),
        tool_calls,
        workspace,
        start_msg_idx=turns['turn:1']['start_msg_idx'],
        end_msg_idx=turns['turn:1']['end_msg_idx'],
    )
    assert turn1_paths == ['report.md']

    turn4_paths = _extract_turn_artifact_paths(
        _turn_message_slice(messages, 'turn:4'),
        tool_calls,
        workspace,
        start_msg_idx=turns['turn:4']['start_msg_idx'],
        end_msg_idx=turns['turn:4']['end_msg_idx'],
    )
    assert turn4_paths == []


def test_extract_manifest_delta_from_turn_reconcile_multi_turn_key(tmp_path):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    docx = workspace / '微博热搜榜_20260612.docx'
    docx.write_bytes(b'fake-docx')
    filler = []
    for i in range(10):
        filler.extend([
            {'role': 'user', 'content': f'q{i}'},
            {'role': 'assistant', 'content': f'a{i}'},
        ])
    messages = filler + [
        {'role': 'user', 'content': '给我一个word'},
        {'role': 'assistant', 'content': f'📄 文件路径：{docx}'},
    ]
    delta = extract_manifest_delta_from_turn_reconcile(
        messages,
        workspace,
        turn_key='turn:20',
        sequence=3,
    )
    assert delta['source']['tool'] == TURN_RECONCILE_SOURCE
    assert delta['artifacts'] == [{
        'path': '微博热搜榜_20260612.docx',
        'preview': MANIFEST_PREVIEW_FILE,
        'source_tool': ASSISTANT_PROSE_ARTIFACT_SOURCE,
    }]


def test_build_session_manifest_assistant_delivery_context_backtick_path(tmp_path, monkeypatch):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    docx = workspace / 'notes' / 'report.docx'
    docx.parent.mkdir()
    docx.write_bytes(b'fake-docx')
    session = Session(
        session_id='assistant_delivery_context01',
        workspace=str(workspace),
        messages=[
            {'role': 'user', 'content': '给我一个word版本吧'},
            {
                'role': 'assistant',
                'content': f'Word 版已生成，结构完整。\n\n📄 **`{docx}`**（18.5 KB）',
            },
        ],
        tool_calls=[],
    )
    session.save()
    monkeypatch.setattr('api.models.SESSION_DIR', tmp_path)
    monkeypatch.setattr('api.models.get_state_db_session_messages', lambda *a, **k: [])
    monkeypatch.setattr('api.session_manifest._load_display_messages', lambda s: list(s.messages))
    manifest = build_session_manifest(session)
    assert manifest['artifacts'] == [{
        'path': 'notes/report.docx',
        'preview': MANIFEST_PREVIEW_FILE,
        'source_tool': ASSISTANT_PROSE_ARTIFACT_SOURCE,
    }]
    assert manifest['turns'][0]['artifacts'] == manifest['artifacts']


def test_build_session_manifest_terminal_pandoc_output_arg_artifact(tmp_path, monkeypatch):
    workspace = tmp_path / 'ws'
    notes = workspace / 'notes'
    notes.mkdir(parents=True)
    (notes / 'report.md').write_text('# Report', encoding='utf-8')
    docx = notes / 'report.docx'
    docx.write_bytes(b'fake-docx')
    command = f'cd {notes} && pandoc report.md -o report.docx --toc'
    session = Session(
        session_id='terminal_pandoc_output01',
        workspace=str(workspace),
        messages=[
            {'role': 'user', 'content': '转成 word'},
            {
                'role': 'assistant',
                'tool_calls': [{
                    'id': 'c1',
                    'function': {'name': 'terminal', 'arguments': json.dumps({'command': command})},
                }],
            },
            {
                'role': 'tool',
                'tool_call_id': 'c1',
                'name': 'terminal',
                'content': 'report.docx: Microsoft Word 2007+',
            },
            {'role': 'assistant', 'content': '文件生成成功。'},
        ],
        tool_calls=[],
    )
    session.save()
    monkeypatch.setattr('api.models.SESSION_DIR', tmp_path)
    monkeypatch.setattr('api.models.get_state_db_session_messages', lambda *a, **k: [])
    monkeypatch.setattr('api.session_manifest._load_display_messages', lambda s: list(s.messages))
    manifest = build_session_manifest(session)
    assert manifest['artifacts'] == [{
        'path': 'notes/report.docx',
        'preview': MANIFEST_PREVIEW_FILE,
        'source_tool': 'terminal',
    }]
    assert manifest['turns'][0]['artifacts'] == manifest['artifacts']


def test_build_session_manifest_terminal_ls_path_candidate_not_artifact(tmp_path, monkeypatch):
    workspace = tmp_path / 'ws'
    notes = workspace / 'notes'
    notes.mkdir(parents=True)
    docx = notes / 'report.docx'
    docx.write_bytes(b'fake-docx')
    session = Session(
        session_id='terminal_ls_negative01',
        workspace=str(workspace),
        messages=[
            {'role': 'user', 'content': '看看目录'},
            {
                'role': 'assistant',
                'tool_calls': [{
                    'id': 'c1',
                    'function': {'name': 'terminal', 'arguments': json.dumps({'command': f'cd {notes} && ls -la'})},
                }],
            },
            {
                'role': 'tool',
                'tool_call_id': 'c1',
                'name': 'terminal',
                'content': '-rw-r--r--  1 user  staff  18578 Jun 12 10:22 report.docx',
            },
            {'role': 'assistant', 'content': f'你可以参考 {docx} 或日志 /var/log/error.log'},
        ],
        tool_calls=[],
    )
    session.save()
    monkeypatch.setattr('api.models.SESSION_DIR', tmp_path)
    monkeypatch.setattr('api.models.get_state_db_session_messages', lambda *a, **k: [])
    monkeypatch.setattr('api.session_manifest._load_display_messages', lambda s: list(s.messages))
    manifest = build_session_manifest(session)
    # ls 输出行本身不产生 artifact，但 assistant 正文提到的路径会匹配
    assert any(
        row['path'] == 'notes/report.docx' and row['source_tool'] == ASSISTANT_PROSE_ARTIFACT_SOURCE
        for row in manifest['artifacts']
    )
    assert manifest['turns'][0]['artifacts']


def test_build_session_manifest_terminal_output_requires_success_and_workspace_file(tmp_path, monkeypatch):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    report = workspace / 'report.pdf'
    report.write_bytes(b'pdf')
    session = Session(
        session_id='terminal_output_failure01',
        workspace=str(workspace),
        messages=[
            {'role': 'user', 'content': '导出 PDF'},
            {'role': 'assistant', 'tool_calls': [{
                'id': 'c1',
                'function': {'name': 'terminal', 'arguments': json.dumps({
                    'command': 'pandoc report.md --output report.pdf',
                })},
            }]},
            {'role': 'tool', 'tool_call_id': 'c1', 'name': 'terminal', 'content': json.dumps({
                'status': 'failed', 'exit_code': 1,
            })},
        ],
        tool_calls=[],
    )
    session.save()
    monkeypatch.setattr('api.models.SESSION_DIR', tmp_path)
    monkeypatch.setattr('api.models.get_state_db_session_messages', lambda *a, **k: [])
    monkeypatch.setattr('api.session_manifest._load_display_messages', lambda s: list(s.messages))
    assert build_session_manifest(session)['artifacts'] == []


def test_build_session_manifest_terminal_output_rejects_external_and_dynamic_paths(tmp_path, monkeypatch):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    session = Session(
        session_id='terminal_output_boundary01',
        workspace=str(workspace),
        messages=[
            {'role': 'user', 'content': '导出 PDF'},
            {'role': 'assistant', 'tool_calls': [{
                'id': 'c1',
                'function': {'name': 'terminal', 'arguments': json.dumps({
                    'command': 'pandoc input.md -o /tmp/report.pdf && pandoc input.md -o "$OUT"',
                })},
            }]},
            {'role': 'tool', 'tool_call_id': 'c1', 'name': 'terminal', 'content': 'ok'},
        ],
        tool_calls=[],
    )
    session.save()
    monkeypatch.setattr('api.models.SESSION_DIR', tmp_path)
    monkeypatch.setattr('api.models.get_state_db_session_messages', lambda *a, **k: [])
    monkeypatch.setattr('api.session_manifest._load_display_messages', lambda s: list(s.messages))
    assert build_session_manifest(session)['artifacts'] == []


def test_build_session_manifest_execute_code_delivery_output(tmp_path, monkeypatch):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    report = workspace / 'report.docx'
    report.write_bytes(b'x')
    sid = 'execute_code_delivery01'
    session = Session(
        session_id=sid,
        workspace=str(workspace),
        messages=[
            {'role': 'user', 'content': '生成文档'},
            {
                'role': 'assistant',
                'tool_calls': [{
                    'id': 'c1',
                    'function': {'name': 'execute_code', 'arguments': json.dumps({'code': '...'})},
                }],
            },
            {
                'role': 'tool',
                'tool_call_id': 'c1',
                'name': 'execute_code',
                'content': json.dumps({
                    'status': 'success',
                    'output': f'✅ 已保存: {report}\n',
                }),
            },
            {'role': 'assistant', 'content': '文档已生成。'},
        ],
        tool_calls=[],
    )
    session.save()
    monkeypatch.setattr('api.models.SESSION_DIR', tmp_path)
    monkeypatch.setattr('api.models.get_state_db_session_messages', lambda *a, **k: [])
    monkeypatch.setattr('api.session_manifest._load_display_messages', lambda s: list(s.messages))
    manifest = build_session_manifest(session)
    assert manifest['artifacts'] == []
    assert manifest['references'] == []


def test_build_session_manifest_execute_code_delivery_skips_missing_and_external(tmp_path, monkeypatch):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    sid = 'execute_code_delivery_neg01'
    session = Session(
        session_id=sid,
        workspace=str(workspace),
        messages=[
            {'role': 'user', 'content': '生成文档'},
            {
                'role': 'assistant',
                'tool_calls': [{
                    'id': 'c1',
                    'function': {'name': 'execute_code', 'arguments': json.dumps({'code': '...'})},
                }],
            },
            {
                'role': 'tool',
                'tool_call_id': 'c1',
                'name': 'execute_code',
                'content': json.dumps({
                    'status': 'success',
                    'output': (
                        '已保存: /tmp/missing.docx\n'
                        '输出文件：/etc/hosts\n'
                        'grep hit: src/main.py\n'
                    ),
                }),
            },
            {'role': 'assistant', 'content': '可以参考 /tmp/a.docx'},
        ],
        tool_calls=[],
    )
    session.save()
    monkeypatch.setattr('api.models.SESSION_DIR', tmp_path)
    monkeypatch.setattr('api.models.get_state_db_session_messages', lambda *a, **k: [])
    monkeypatch.setattr('api.session_manifest._load_display_messages', lambda s: list(s.messages))
    manifest = build_session_manifest(session)
    assert manifest['artifacts'] == []
    assert manifest['references'] == []


# ── 改动一：assistant 正文宽泛正则路径直接提升为成果 ──


def test_build_session_manifest_absolute_path_without_delivery_context(tmp_path, monkeypatch):
    """绝对路径在 assistant 正文中（无交付关键词），文件存在 → artifacts"""
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    output_pdf = workspace / 'analysis_report.pdf'
    output_pdf.write_bytes(b'pdf-content')
    sid = 'abs_path_no_ctx01'
    session = Session(
        session_id=sid,
        workspace=str(workspace),
        messages=[
            {'role': 'user', 'content': '帮我生成报告'},
            {
                'role': 'assistant',
                'content': f'报告已生成，可以查看 {output_pdf}',
            },
        ],
        tool_calls=[],
    )
    session.save()
    monkeypatch.setattr('api.models.SESSION_DIR', tmp_path)
    monkeypatch.setattr('api.models.get_state_db_session_messages', lambda *a, **k: [])
    monkeypatch.setattr('api.session_manifest._load_display_messages', lambda s: list(s.messages))
    manifest = build_session_manifest(session)
    assert any(
        row['path'] == 'analysis_report.pdf' and row['source_tool'] == ASSISTANT_PROSE_ARTIFACT_SOURCE
        for row in manifest['artifacts']
    )


def test_build_session_manifest_final_relative_path_without_delivery_context(tmp_path, monkeypatch):
    """最后一条 assistant 中的既存相对路径不依赖交付关键词。"""
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    src_dir = workspace / 'src'
    src_dir.mkdir()
    main_py = src_dir / 'main.py'
    main_py.write_text('print(1)', encoding='utf-8')
    sid = 'rel_path_no_ctx01'
    session = Session(
        session_id=sid,
        workspace=str(workspace),
        messages=[
            {'role': 'user', 'content': '写代码'},
            {
                'role': 'assistant',
                'content': '入口文件在 ./src/main.py，你可以看下',
            },
        ],
        tool_calls=[],
    )
    session.save()
    monkeypatch.setattr('api.models.SESSION_DIR', tmp_path)
    monkeypatch.setattr('api.models.get_state_db_session_messages', lambda *a, **k: [])
    monkeypatch.setattr('api.session_manifest._load_display_messages', lambda s: list(s.messages))
    manifest = build_session_manifest(session)
    assert any(
        row['path'] == 'src/main.py' and row['source_tool'] == ASSISTANT_PROSE_ARTIFACT_SOURCE
        for row in manifest['artifacts']
    )


def test_build_session_manifest_final_code_span_without_delivery_context(tmp_path, monkeypatch):
    """最后一条 assistant 中的既存反引号文件名成为 artifact。"""
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    result_json = workspace / 'result.json'
    result_json.write_text('{}', encoding='utf-8')
    sid = 'code_span_no_ctx01'
    session = Session(
        session_id=sid,
        workspace=str(workspace),
        messages=[
            {'role': 'user', 'content': '生成json'},
            {
                'role': 'assistant',
                'content': '输出已经写入 `result.json`，可以看看',
            },
        ],
        tool_calls=[],
    )
    session.save()
    monkeypatch.setattr('api.models.SESSION_DIR', tmp_path)
    monkeypatch.setattr('api.models.get_state_db_session_messages', lambda *a, **k: [])
    monkeypatch.setattr('api.session_manifest._load_display_messages', lambda s: list(s.messages))
    manifest = build_session_manifest(session)
    assert any(
        row['path'] == 'result.json' and row['source_tool'] == ASSISTANT_PROSE_ARTIFACT_SOURCE
        for row in manifest['artifacts']
    )


def test_build_session_manifest_final_basename_without_delivery_context_is_artifact(tmp_path, monkeypatch):
    """最后一条 assistant 中的既存裸文件名成为 artifact。"""
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    data_csv = workspace / 'data.csv'
    data_csv.write_text('a,b,c', encoding='utf-8')
    sid = 'basename_no_ctx01'
    session = Session(
        session_id=sid,
        workspace=str(workspace),
        messages=[
            {'role': 'user', 'content': '处理数据'},
            {
                'role': 'assistant',
                'content': '你可以用 data.csv 来测试',
            },
        ],
        tool_calls=[],
    )
    session.save()
    monkeypatch.setattr('api.models.SESSION_DIR', tmp_path)
    monkeypatch.setattr('api.models.get_state_db_session_messages', lambda *a, **k: [])
    monkeypatch.setattr('api.session_manifest._load_display_messages', lambda s: list(s.messages))
    manifest = build_session_manifest(session)
    assert manifest['artifacts'] == [{
        'path': 'data.csv',
        'preview': MANIFEST_PREVIEW_FILE,
        'source_tool': ASSISTANT_PROSE_ARTIFACT_SOURCE,
    }]


def test_build_session_manifest_path_missing_file_not_artifact(tmp_path, monkeypatch):
    """正则匹配到的路径文件不存在 → 不出 artifacts"""
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    sid = 'missing_file_no_ctx01'
    session = Session(
        session_id=sid,
        workspace=str(workspace),
        messages=[
            {'role': 'user', 'content': '生成报告'},
            {
                'role': 'assistant',
                'content': '报告在 /tmp/nonexistent/report.docx',
            },
        ],
        tool_calls=[],
    )
    session.save()
    monkeypatch.setattr('api.models.SESSION_DIR', tmp_path)
    monkeypatch.setattr('api.models.get_state_db_session_messages', lambda *a, **k: [])
    monkeypatch.setattr('api.session_manifest._load_display_messages', lambda s: list(s.messages))
    manifest = build_session_manifest(session)
    assert manifest['artifacts'] == []


# ── 文件读取仅作为瞬态 artifact 排除证据，不公开为 reference ──


def test_build_session_manifest_read_file_is_not_public_reference(tmp_path, monkeypatch):
    """成功 read_file 不进入公开 references 或 artifacts。"""
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    (workspace / 'config.yaml').write_text('key: val', encoding='utf-8')
    sid = 'read_file_ref01'
    session = Session(
        session_id=sid,
        workspace=str(workspace),
        messages=[
            {'role': 'user', 'content': '读取配置', '_turn_key': 'turn:0'},
            {
                'role': 'assistant',
                'tool_calls': [{
                    'id': 'c1',
                    'function': {
                        'name': 'read_file',
                        'arguments': json.dumps({'path': 'config.yaml'}),
                    },
                }],
            },
            {'role': 'tool', 'tool_call_id': 'c1', 'content': 'key: val'},
        ],
        tool_calls=[],
    )
    session.save()
    monkeypatch.setattr('api.models.SESSION_DIR', tmp_path)
    monkeypatch.setattr('api.models.get_state_db_session_messages', lambda *a, **k: [])
    monkeypatch.setattr('api.session_manifest._load_display_messages', lambda s: list(s.messages))
    manifest = build_session_manifest(session)
    assert manifest['artifacts'] == []
    assert manifest['references'] == []
    assert manifest['turns'][0]['artifacts'] == []
    assert manifest['turns'][0]['references'] == []


def test_build_session_manifest_missing_read_file_has_no_expired_reference(tmp_path, monkeypatch):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    sid = 'read_file_expired01'
    session = Session(
        session_id=sid,
        workspace=str(workspace),
        messages=[
            {'role': 'user', 'content': '读取配置', '_turn_key': 'turn:0'},
            {
                'role': 'assistant',
                'tool_calls': [{
                    'id': 'c1',
                    'function': {
                        'name': 'read_file',
                        'arguments': json.dumps({'path': 'gone.yaml'}),
                    },
                }],
            },
            {'role': 'tool', 'tool_call_id': 'c1', 'content': 'error'},
        ],
        tool_calls=[],
    )
    session.save()
    monkeypatch.setattr('api.models.SESSION_DIR', tmp_path)
    monkeypatch.setattr('api.models.get_state_db_session_messages', lambda *a, **k: [])
    monkeypatch.setattr('api.session_manifest._load_display_messages', lambda s: list(s.messages))
    manifest = build_session_manifest(session)
    assert manifest['references'] == []
    assert manifest['turns'][0]['references'] == []


def test_build_session_manifest_list_dir_no_reference(tmp_path, monkeypatch):
    """list_dir 列出目录 → 不出现 references"""
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    (workspace / 'sub').mkdir()
    sid = 'list_dir_no_ref01'
    session = Session(
        session_id=sid,
        workspace=str(workspace),
        messages=[
            {'role': 'user', 'content': '列出目录'},
            {
                'role': 'assistant',
                'tool_calls': [{
                    'id': 'c1',
                    'function': {
                        'name': 'list_dir',
                        'arguments': json.dumps({'path': 'sub'}),
                    },
                }],
            },
            {'role': 'tool', 'tool_call_id': 'c1', 'content': 'file1.txt'},
        ],
        tool_calls=[],
    )
    session.save()
    monkeypatch.setattr('api.models.SESSION_DIR', tmp_path)
    monkeypatch.setattr('api.models.get_state_db_session_messages', lambda *a, **k: [])
    monkeypatch.setattr('api.session_manifest._load_display_messages', lambda s: list(s.messages))
    manifest = build_session_manifest(session)
    assert manifest['references'] == []
    assert manifest['turns'][0]['references'] == []


def test_next_turn_key_empty_messages():
    """空消息列表 → turn:1"""
    assert _next_turn_key([]) == 'turn:1'


def test_next_turn_key_scans_existing():
    """扫描现有消息中最大 _turn_key """
    messages = [
        {'role': 'user', 'content': 'a', '_turn_key': 'turn:5'},
        {'role': 'assistant', 'content': 'ok'},
        {'role': 'user', 'content': 'b', '_turn_key': 'turn:3'},
    ]
    assert _next_turn_key(messages) == 'turn:6'


def test_next_turn_key_ignores_non_user():
    """忽略非 user 角色的消息"""
    messages = [
        {'role': 'assistant', 'content': 'x', '_turn_key': 'turn:99'},
    ]
    assert _next_turn_key(messages) == 'turn:1'


def test_next_turn_key_handles_invalid_key():
    """跳过无效的 _turn_key 值"""
    messages = [
        {'role': 'user', 'content': 'a', '_turn_key': 'not_a_turn'},
        {'role': 'user', 'content': 'b', '_turn_key': 'turn:abc'},
    ]
    assert _next_turn_key(messages) == 'turn:1'


def test_message_turns_preserves_stamped_turn_key():
    """_message_turns() 优先使用用户消息中的 _turn_key"""
    messages = [
        {'role': 'user', 'content': 'q1', '_turn_key': 'turn:3'},
        {'role': 'assistant', 'content': 'a1'},
        {'role': 'user', 'content': 'q2', '_turn_key': 'turn:7'},
        {'role': 'assistant', 'content': 'a2'},
    ]
    turns = _message_turns(messages)
    assert len(turns) == 2
    assert turns[0]['turn_key'] == 'turn:3'
    assert turns[0]['user_msg_idx'] == 0
    assert turns[1]['turn_key'] == 'turn:7'
    assert turns[1]['user_msg_idx'] == 2


def test_message_turns_falls_back_to_index():
    """无 _turn_key 时降级为索引 key（向后兼容）"""
    messages = [
        {'role': 'user', 'content': 'q1'},
        {'role': 'assistant', 'content': 'a1'},
        {'role': 'user', 'content': 'q2'},
        {'role': 'assistant', 'content': 'a2'},
    ]
    turns = _message_turns(messages)
    assert len(turns) == 2
    assert turns[0]['turn_key'] == 'turn:0'
    assert turns[1]['turn_key'] == 'turn:2'


def test_ensure_turn_keys_does_not_invent_active_turn_numbers():
    """Manifest GET must not turn a mixed transcript into a new valid turn."""
    messages = [
        {'role': 'user', 'content': 'q1', '_turn_key': 'turn:5'},
        {'role': 'assistant', 'content': 'a1'},
        {'role': 'user', 'content': 'q2'},  # 缺失
        {'role': 'user', 'content': 'q3', '_turn_key': 'turn:7'},
        {'role': 'user', 'content': 'q4'},  # 缺失
    ]
    original = [dict(row) for row in messages]
    result = _ensure_turn_keys(messages)
    assert messages == original
    assert result[2].get('_turn_key') is None
    assert result[4].get('_turn_key') is None


def test_ensure_turn_keys_all_missing():
    """所有用户消息都缺失 _turn_key → 存量会话，不做任何修改"""
    messages = [
        {'role': 'user', 'content': 'q1'},
        {'role': 'assistant', 'content': 'a1'},
        {'role': 'user', 'content': 'q2'},
    ]
    _ensure_turn_keys(messages)
    assert messages[0].get('_turn_key') is None
    assert messages[2].get('_turn_key') is None


def test_build_session_manifest_does_not_invent_turn_for_unkeyed_mixed_segment(tmp_path, monkeypatch):
    """A mixed transcript exposes an unbound segment only through diagnostics."""
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    (workspace / 'unbound.txt').write_text('unbound', encoding='utf-8')
    messages = [
        {'role': 'user', 'content': 'q1', '_turn_key': 'turn:6'},
        {'role': 'assistant', 'content': 'a1'},
        {'role': 'user', 'content': 'q2'},
        {
            'role': 'assistant',
            'tool_calls': [{
                'id': 'unbound-write',
                'function': {
                    'name': 'write_file',
                    'arguments': json.dumps({'path': 'unbound.txt'}),
                },
            }],
        },
        {'role': 'tool', 'tool_call_id': 'unbound-write', 'content': 'ok'},
        {'role': 'user', 'content': 'q3', '_turn_key': 'turn:8'},
        {'role': 'assistant', 'content': 'a3'},
    ]
    session = Session(
        session_id='manifest_mixed_unbound01',
        workspace=str(workspace),
        messages=messages,
        tool_calls=[],
    )
    original = copy.deepcopy(messages)
    monkeypatch.setattr('api.session_manifest._load_display_messages', lambda s: list(s.messages))
    monkeypatch.setattr('api.session_manifest_store.repair_empty_manifest_turns', lambda s: {})
    monkeypatch.setattr(
        'api.session_manifest_store.load_manifest_records',
        lambda s, include_lineage=True: [],
    )
    monkeypatch.setattr(
        'api.session_manifest_store.load_manifest_decided_turn_keys',
        lambda s, include_lineage=True: set(),
    )
    monkeypatch.setattr(
        'api.session_manifest_store.backfill_missing_manifest_records',
        lambda s: {'source': 'derived'},
    )

    manifest = build_session_manifest(session)

    assert [turn['turn_key'] for turn in manifest['turns']] == ['turn:6', 'turn:8']
    assert manifest['diagnostics']['missing_turn_key_message_indices'] == [2]
    assert all(
        artifact['path'] != 'unbound.txt'
        for turn in manifest['turns']
        for artifact in turn['artifacts']
    )
    assert session.messages == original


def test_build_session_manifest_compression_turn_keys(tmp_path, monkeypatch):
    """模拟压缩场景——带有压缩标记的消息，验证 turn key 基于 _turn_key 保持稳定"""
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    (workspace / 'first.txt').write_text('one', encoding='utf-8')
    sid = 'compression_turns01'
    session = Session(
        session_id=sid,
        workspace=str(workspace),
        messages=[
            # 第一轮
            {'role': 'user', 'content': 'q1', '_turn_key': 'turn:1'},
            {'role': 'assistant', 'content': 'a1', 'tool_calls': [{
                'id': 'c1',
                'function': {'name': 'write_file', 'arguments': json.dumps({'path': 'first.txt'})},
            }]},
            {'role': 'tool', 'tool_call_id': 'c1', 'content': 'ok'},
            # 压缩标记（插入后占据一个索引位置）
            {'role': 'user', 'content': '[Context compaction — reference only]'},
            # 第二轮
            {'role': 'user', 'content': 'q2', '_turn_key': 'turn:2'},
            {'role': 'assistant', 'content': 'a2'},
        ],
        tool_calls=[],
    )
    monkeypatch.setattr('api.session_manifest._load_display_messages', lambda s: list(s.messages))
    monkeypatch.setattr('api.models.get_state_db_session_messages', lambda *a, **k: [])

    manifest = build_session_manifest(session)

    # 压缩标记由 is_context_compression_marker() 跳过，不算独立 turn
    assert len(manifest['turns']) == 2
    assert manifest['turns'][0]['turn_key'] == 'turn:1'
    assert manifest['turns'][1]['turn_key'] == 'turn:2'
    assert manifest['turns'][0]['artifacts'][0]['path'] == 'first.txt'
    assert manifest['turns'][1]['artifacts'] == []


def test_session_load_restores_turn_artifacts(tmp_path, monkeypatch):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    sid = 'turn_artifacts_load01'
    session_path = tmp_path / 'sessions' / f'{sid}.json'
    session_path.parent.mkdir(parents=True)
    session_path.write_text(json.dumps({
        'session_id': sid,
        'title': 't',
        'workspace': str(workspace),
        'messages': [],
        'turn_artifacts': {'turn:1': ['report.md']},
    }), encoding='utf-8')
    monkeypatch.setattr('api.models.SESSION_DIR', session_path.parent)

    loaded = Session.load(sid)
    assert loaded is not None
    assert loaded.turn_artifacts == {'turn:1': ['report.md']}


def test_turn_artifacts_for_wire_filters_missing_files(tmp_path):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    (workspace / 'report.md').write_text('# report', encoding='utf-8')
    session = Session(
        session_id='turn_artifacts_wire01',
        workspace=str(workspace),
        turn_artifacts={
            'turn:1': ['report.md'],
            'turn:2': ['make_docx.py', 'make_poster.py', 'report.md'],
        },
    )

    wired = turn_artifacts_for_wire(session)
    assert wired == {
        'turn:1': [{'path': 'report.md', 'source_tool': 'write_file', 'preview': MANIFEST_PREVIEW_FILE}],
        'turn:2': [{'path': 'report.md', 'source_tool': 'write_file', 'preview': MANIFEST_PREVIEW_FILE}],
    }
    assert 'turn_artifacts' not in session.compact()


def test_build_session_manifest_turn_artifacts_match_wire(tmp_path, monkeypatch):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    (workspace / 'report.md').write_text('# report', encoding='utf-8')
    (workspace / 'deliver.docx').write_text('doc', encoding='utf-8')
    session = Session(
        session_id='turn_artifacts_manifest01',
        workspace=str(workspace),
        profile='default',
        messages=[
            {'role': 'user', 'content': 'q1', '_turn_key': 'turn:1'},
            {'role': 'assistant', 'content': 'a1'},
            {'role': 'user', 'content': 'q2', '_turn_key': 'turn:2'},
            {'role': 'assistant', 'content': 'a2'},
        ],
        turn_artifacts={
            'turn:1': ['report.md', 'missing.py'],
            'turn:2': ['make_docx.py', 'deliver.docx'],
        },
    )
    monkeypatch.setattr('api.session_manifest._load_display_messages', lambda s: list(s.messages))

    wired = turn_artifacts_for_wire(session)
    manifest = build_session_manifest(session)

    assert wired == {
        'turn:1': [{'path': 'report.md', 'source_tool': 'write_file', 'preview': MANIFEST_PREVIEW_FILE}],
        'turn:2': [{'path': 'deliver.docx', 'source_tool': 'write_file', 'preview': MANIFEST_PREVIEW_FILE}],
    }
    manifest_by_turn = {
        turn['turn_key']: turn['artifacts']
        for turn in manifest['turns']
    }
    assert [row['path'] for row in manifest_by_turn['turn:1']] == ['missing.py', 'report.md']
    assert manifest_by_turn['turn:1'][0]['status'] == 'expired'
    assert 'status' not in manifest_by_turn['turn:1'][1]
    assert [row['path'] for row in manifest_by_turn['turn:2']] == ['deliver.docx', 'make_docx.py']
    assert manifest_by_turn['turn:2'][1]['status'] == 'expired'


def test_filter_existing_turn_artifact_paths_deduplicates(tmp_path):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    (workspace / 'report.md').write_text('# report', encoding='utf-8')

    filtered = filter_existing_turn_artifact_paths(
        workspace,
        ['report.md', 'report.md', 'missing.py', ''],
    )
    assert filtered == ['report.md']


def test_build_session_manifest_groups_references_by_turn(tmp_path, monkeypatch):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    skills_dir = tmp_path / 'profile-home' / 'skills'
    _write_local_skill(skills_dir, 'skill-a')
    _write_local_skill(skills_dir, 'skill-b')
    session = Session(
        session_id='manifestrefs01',
        workspace=str(workspace),
        messages=[
            {'role': 'user', 'content': 'read a', '_turn_key': 'turn:1'},
            {
                'role': 'assistant',
                'tool_calls': [{
                    'id': 'c1',
                    'function': {
                        'name': 'skill_view',
                        'arguments': json.dumps({'name': 'skill-a'}),
                    },
                }],
            },
            {'role': 'tool', 'tool_call_id': 'c1', 'content': json.dumps({
                'success': True,
                'name': 'skill-a',
                'path': str(skills_dir / 'skill-a' / 'SKILL.md'),
            })},
            {'role': 'user', 'content': 'read b', '_turn_key': 'turn:2'},
            {
                'role': 'assistant',
                'tool_calls': [{
                    'id': 'c2',
                    'function': {
                        'name': 'skill_view',
                        'arguments': json.dumps({'name': 'skill-b'}),
                    },
                }],
            },
            {'role': 'tool', 'tool_call_id': 'c2', 'content': json.dumps({
                'success': True,
                'name': 'skill-b',
                'path': str(skills_dir / 'skill-b' / 'SKILL.md'),
            })},
        ],
        tool_calls=[],
    )
    monkeypatch.setattr('api.session_manifest._load_display_messages', lambda s: list(s.messages))
    monkeypatch.setattr('api.session_manifest._skills_dir_for_session', lambda s: skills_dir)
    monkeypatch.setattr('api.session_manifest._skillhub_preview_available', lambda: True)

    manifest = build_session_manifest(session)

    assert len(manifest['turns']) == 2
    first_refs = manifest['turns'][0]['references']
    second_refs = manifest['turns'][1]['references']
    assert [row['path'] for row in first_refs] == ['skill-a']
    assert [row['path'] for row in second_refs] == ['skill-b']
    assert {row['path'] for row in manifest['references']} == {'skill-a', 'skill-b'}


def test_persist_turn_artifact_paths_filters_missing_files(tmp_path, monkeypatch):
    from api.streaming import _persist_turn_artifact_paths

    workspace = tmp_path / 'ws'
    workspace.mkdir()
    (workspace / 'deliver.docx').write_text('doc', encoding='utf-8')
    session = Session(
        session_id='persist_filter01',
        workspace=str(workspace),
        messages=[
            {'role': 'user', 'content': 'q1', '_turn_key': 'turn:1'},
            {'role': 'assistant', 'content': 'a1'},
            {'role': 'user', 'content': 'q2', '_turn_key': 'turn:2'},
            {
                'role': 'assistant',
                'tool_calls': [
                    {
                        'id': 'c2',
                        'function': {
                            'name': 'write_file',
                            'arguments': json.dumps({'path': 'deliver.docx'}),
                        },
                    },
                    {
                        'id': 'c3',
                        'function': {
                            'name': 'write_file',
                            'arguments': json.dumps({'path': 'make_docx.py'}),
                        },
                    },
                ],
            },
            {'role': 'tool', 'tool_call_id': 'c2', 'content': 'ok'},
            {'role': 'tool', 'tool_call_id': 'c3', 'content': 'ok'},
        ],
        tool_calls=[],
    )

    monkeypatch.setattr('api.session_manifest_store.STATE_DIR', tmp_path / 'state')

    _persist_turn_artifact_paths(session, 'turn:2')

    from api.session_manifest_store import load_manifest_records

    records = load_manifest_records(session)
    assert [(row['turn_key'], row['path'], row['source_tool'], row['preview']) for row in records] == [
        ('turn:2', 'deliver.docx', 'write_file', MANIFEST_PREVIEW_FILE),
    ]
    assert session.turn_artifacts == {}


def test_persist_turn_artifact_paths_scopes_session_tool_calls(tmp_path, monkeypatch):
    from api.streaming import _persist_turn_artifact_paths

    workspace = tmp_path / 'ws'
    workspace.mkdir()
    (workspace / 'report.md').write_text('# report', encoding='utf-8')
    (workspace / 'deliver.docx').write_text('doc', encoding='utf-8')
    session = Session(
        session_id='persist_scope01',
        workspace=str(workspace),
        messages=[
            {'role': 'user', 'content': 'q1', '_turn_key': 'turn:1'},
            {'role': 'assistant', 'content': 'a1'},
            {'role': 'user', 'content': 'q2', '_turn_key': 'turn:2'},
            {'role': 'assistant', 'content': 'a2'},
        ],
        tool_calls=[
            {
                'name': 'write_file',
                'args': {'path': 'report.md'},
                'assistant_msg_idx': 1,
                'tid': 'write-turn1',
                'done': True,
            },
            {
                'name': 'write_file',
                'args': {'path': 'deliver.docx'},
                'assistant_msg_idx': 3,
                'tid': 'write-turn2',
                'done': True,
            },
        ],
    )

    monkeypatch.setattr('api.session_manifest_store.STATE_DIR', tmp_path / 'state')

    _persist_turn_artifact_paths(session, 'turn:2')

    from api.session_manifest_store import load_manifest_records

    records = load_manifest_records(session)
    assert [(row['turn_key'], row['path'], row['source_tool'], row['preview']) for row in records] == [
        ('turn:2', 'deliver.docx', 'write_file', MANIFEST_PREVIEW_FILE),
    ]
    assert session.turn_artifacts == {}


def test_persist_turn_artifact_paths_keeps_same_path_across_turns(tmp_path, monkeypatch):
    from api.streaming import _persist_turn_artifact_paths

    workspace = tmp_path / 'ws'
    workspace.mkdir()
    artifact = workspace / 'worldcup-poster.png'
    artifact.write_bytes(b'png')
    abs_path = artifact.as_posix()
    session = Session(
        session_id='persist_cross_turn_keep01',
        workspace=str(workspace),
        messages=[
            {'role': 'user', 'content': 'turn1', '_turn_key': 'turn:1'},
            {'role': 'assistant', 'content': f'已生成\n\nMEDIA:{abs_path}'},
            {'role': 'user', 'content': 'turn2', '_turn_key': 'turn:2'},
            {'role': 'assistant', 'content': f'重生成\n\nMEDIA:{abs_path}'},
        ],
        tool_calls=[],
    )

    monkeypatch.setattr('api.session_manifest_store.STATE_DIR', tmp_path / 'state')

    _persist_turn_artifact_paths(session, 'turn:1')
    _persist_turn_artifact_paths(session, 'turn:2')

    from api.session_manifest_store import load_manifest_records

    records = load_manifest_records(session)
    assert [(row['turn_key'], row['path'], row['source_tool'], row['preview']) for row in records] == [
        ('turn:1', 'worldcup-poster.png', MEDIA_ARTIFACT_SOURCE, MANIFEST_PREVIEW_FILE),
        ('turn:2', 'worldcup-poster.png', MEDIA_ARTIFACT_SOURCE, MANIFEST_PREVIEW_FILE),
    ]
    assert session.turn_artifacts == {}


def test_session_get_omits_turn_artifacts_and_manifest_is_authoritative(tmp_path, monkeypatch):
    from urllib.parse import urlparse

    import api.routes as routes

    workspace = tmp_path / 'ws'
    workspace.mkdir()
    (workspace / 'report.md').write_text('# report', encoding='utf-8')
    (workspace / 'deliver.docx').write_text('doc', encoding='utf-8')
    session = Session(
        session_id='http_align01',
        workspace=str(workspace),
        profile='default',
        messages=[
            {'role': 'user', 'content': 'q1', '_turn_key': 'turn:1'},
            {'role': 'assistant', 'content': 'a1'},
            {'role': 'user', 'content': 'q2', '_turn_key': 'turn:2'},
            {'role': 'assistant', 'content': 'a2'},
        ],
        turn_artifacts={
            'turn:1': ['report.md', 'missing.py'],
            'turn:2': ['deliver.docx', 'make_docx.py'],
        },
    )
    monkeypatch.setattr(routes, 'get_session', lambda sid, metadata_only=False: session)
    monkeypatch.setattr(routes, '_clear_stale_stream_state', lambda _s: None)
    monkeypatch.setattr(routes, 'redact_session_data', lambda payload: payload)
    monkeypatch.setattr(routes, 'j', lambda _handler, payload, status=200, extra_headers=None: payload)
    monkeypatch.setattr('api.session_manifest._load_display_messages', lambda s: list(s.messages))
    monkeypatch.setattr('api.session_manifest._skills_dir_for_session', lambda s: tmp_path / 'skills')
    monkeypatch.setattr('api.session_manifest._skillhub_preview_available', lambda: False)
    monkeypatch.setattr('api.session_manifest_store.STATE_DIR', tmp_path / 'state')

    session_resp = routes.handle_get(
        object(),
        urlparse('/api/session?session_id=http_align01&messages=0&resolve_model=0'),
    )
    manifest_resp = routes.handle_get(
        object(),
        urlparse('/api/session/manifest?session_id=http_align01'),
    )

    assert 'turn_artifacts' not in session_resp['session']
    assert manifest_resp['manifest_source'] == 'backfill'
    manifest_by_turn = {
        turn['turn_key']: turn['artifacts']
        for turn in manifest_resp['manifest']['turns']
    }
    assert [row['path'] for row in manifest_by_turn['turn:1']] == ['missing.py', 'report.md']
    assert manifest_by_turn['turn:1'][0]['status'] == 'expired'
    assert [row['path'] for row in manifest_by_turn['turn:2']] == ['deliver.docx', 'make_docx.py']
    assert manifest_by_turn['turn:2'][1]['status'] == 'expired'


def test_build_session_manifest_multi_turn_mixed_artifacts_and_references(tmp_path, monkeypatch):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    skills_dir = tmp_path / 'profile-home' / 'skills'
    _write_local_skill(skills_dir, 'docx-generation')
    (workspace / 'report.md').write_text('# report', encoding='utf-8')
    (workspace / 'deliver.docx').write_text('doc', encoding='utf-8')
    session = Session(
        session_id='mixed_turn_manifest01',
        workspace=str(workspace),
        profile='default',
        messages=[
            {'role': 'user', 'content': 'write report', '_turn_key': 'turn:1'},
            {
                'role': 'assistant',
                'tool_calls': [{
                    'id': 'c1',
                    'function': {
                        'name': 'write_file',
                        'arguments': json.dumps({'path': 'report.md'}),
                    },
                }],
            },
            {'role': 'tool', 'tool_call_id': 'c1', 'content': 'ok'},
            {'role': 'user', 'content': 'make docx', '_turn_key': 'turn:2'},
            {
                'role': 'assistant',
                'tool_calls': [{
                    'id': 'c2',
                    'function': {
                        'name': 'skill_view',
                        'arguments': json.dumps({'name': 'docx-generation'}),
                    },
                }],
            },
            {'role': 'tool', 'tool_call_id': 'c2', 'content': json.dumps({
                'success': True,
                'name': 'docx-generation',
                'path': str(skills_dir / 'docx-generation' / 'SKILL.md'),
            })},
        ],
        turn_artifacts={
            'turn:1': ['report.md', 'missing.py'],
            'turn:2': ['deliver.docx', 'make_docx.py'],
        },
        tool_calls=[],
    )
    monkeypatch.setattr('api.session_manifest._load_display_messages', lambda s: list(s.messages))
    monkeypatch.setattr('api.session_manifest._skills_dir_for_session', lambda s: skills_dir)
    monkeypatch.setattr('api.session_manifest._skillhub_preview_available', lambda: True)

    wired = turn_artifacts_for_wire(session)
    manifest = build_session_manifest(session)
    manifest_by_turn = {
        turn['turn_key']: {
            'artifacts': list(turn['artifacts']),
            'references': list(turn['references']),
        }
        for turn in manifest['turns']
    }

    assert wired == {
        'turn:1': [{'path': 'report.md', 'source_tool': 'write_file', 'preview': MANIFEST_PREVIEW_FILE}],
        'turn:2': [{'path': 'deliver.docx', 'source_tool': 'write_file', 'preview': MANIFEST_PREVIEW_FILE}],
    }
    turn1_paths = [row['path'] for row in manifest_by_turn['turn:1']['artifacts']]
    assert turn1_paths == ['missing.py', 'report.md']
    assert manifest_by_turn['turn:1']['artifacts'][0]['status'] == 'expired'
    assert 'status' not in manifest_by_turn['turn:1']['artifacts'][1]
    assert manifest_by_turn['turn:1']['references'] == []
    turn2_paths = [row['path'] for row in manifest_by_turn['turn:2']['artifacts']]
    assert turn2_paths == ['deliver.docx', 'make_docx.py']
    assert manifest_by_turn['turn:2']['artifacts'][1]['status'] == 'expired'
    assert manifest_by_turn['turn:2']['references'][0]['path'] == 'docx-generation'


def test_persist_turn_artifact_paths_includes_skill_manage(tmp_path, monkeypatch):
    from api.streaming import _persist_turn_artifact_paths

    workspace = tmp_path / 'ws'
    workspace.mkdir()
    skills_dir = tmp_path / 'profile-home' / 'skills'
    _write_local_skill(skills_dir, 'foo', rel_path='research/foo')
    session = Session(
        session_id='persist_skill01',
        workspace=str(workspace),
        profile='test-profile',
        messages=[
            {'role': 'user', 'content': 'create skill', '_turn_key': 'turn:1'},
            {
                'role': 'assistant',
                'tool_calls': [{
                    'id': 'c1',
                    'function': {
                        'name': 'skill_manage',
                        'arguments': json.dumps({
                            'action': 'create',
                            'name': 'foo',
                            'content': '# Skill',
                        }),
                    },
                }],
            },
            {
                'role': 'tool',
                'tool_call_id': 'c1',
                'content': json.dumps({'success': True, 'path': 'research/foo'}),
            },
        ],
        tool_calls=[],
    )
    monkeypatch.setattr('api.session_manifest._skills_dir_for_session', lambda s: skills_dir)
    monkeypatch.setattr('api.session_manifest._skillhub_preview_available', lambda: True)
    monkeypatch.setattr('api.session_manifest_store.STATE_DIR', tmp_path / 'state')

    _persist_turn_artifact_paths(session, 'turn:1')

    from api.session_manifest_store import load_manifest_records

    records = load_manifest_records(session)
    assert [(row['turn_key'], row['path'], row['source_tool'], row['preview']) for row in records] == [
        ('turn:1', 'research/foo', 'skill_manage', MANIFEST_PREVIEW_SKILL),
    ]
    assert session.turn_artifacts == {}


def test_persist_turn_artifact_paths_empty_turn_not_stored(tmp_path, monkeypatch):
    from api.streaming import _persist_turn_artifact_paths

    workspace = tmp_path / 'ws'
    workspace.mkdir()
    session = Session(
        session_id='persist_empty01',
        workspace=str(workspace),
        messages=[
            {'role': 'user', 'content': 'hello', '_turn_key': 'turn:1'},
            {'role': 'assistant', 'content': 'hi'},
        ],
        tool_calls=[],
    )
    monkeypatch.setattr('api.session_manifest._skills_dir_for_session', lambda s: tmp_path / 'skills')
    monkeypatch.setattr('api.session_manifest_store.STATE_DIR', tmp_path / 'state')

    _persist_turn_artifact_paths(session, 'turn:1')

    from api.session_manifest_store import load_manifest_decided_turn_keys, load_manifest_records

    assert load_manifest_records(session) == []
    assert load_manifest_decided_turn_keys(session) == {'turn:1'}
    assert session.turn_artifacts == {}


def test_build_session_manifest_skill_manage_with_empty_persisted_turn_artifacts(tmp_path, monkeypatch):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    skills_dir = tmp_path / 'profile-home' / 'skills'
    _write_local_skill(skills_dir, 'turn-skill', rel_path='research/turn-skill')
    session = Session(
        session_id='manifestskill_empty_persist01',
        workspace=str(workspace),
        profile='test-profile',
        messages=[
            {'role': 'user', 'content': 'create skill', '_turn_key': 'turn:1'},
            {
                'role': 'assistant',
                'tool_calls': [{
                    'id': 'c1',
                    'function': {
                        'name': 'skill_manage',
                        'arguments': json.dumps({
                            'action': 'create',
                            'name': 'turn-skill',
                            'content': '# Skill',
                        }),
                    },
                }],
            },
            {
                'role': 'tool',
                'tool_call_id': 'c1',
                'content': json.dumps({'success': True, 'path': 'research/turn-skill'}),
            },
        ],
        turn_artifacts={'turn:1': []},
        tool_calls=[],
    )
    monkeypatch.setattr('api.session_manifest._load_display_messages', lambda s: list(s.messages))
    monkeypatch.setattr('api.session_manifest._skills_dir_for_session', lambda s: skills_dir)
    monkeypatch.setattr('api.session_manifest._skillhub_preview_available', lambda: True)

    manifest = build_session_manifest(session)

    assert manifest['turns'][0]['artifacts'] == [{
        'path': 'research/turn-skill',
        'preview': MANIFEST_PREVIEW_SKILL,
        'source_tool': 'skill_manage',
        'profile': 'test-profile',
    }]


def test_build_session_manifest_skill_view_deduped_when_artifact(tmp_path, monkeypatch):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    skills_dir = tmp_path / 'profile-home' / 'skills'
    _write_local_skill(skills_dir, 'ai-news-top10', rel_path='research/ai-news-top10')
    _write_local_skill(skills_dir, 'hermes-agent-skill-authoring')
    session = Session(
        session_id='manifestskill_dedup01',
        workspace=str(workspace),
        profile='default',
        messages=[
            {'role': 'user', 'content': 'create skill', '_turn_key': 'turn:1'},
            {
                'role': 'assistant',
                'tool_calls': [
                    {
                        'id': 'c1',
                        'function': {
                            'name': 'skill_view',
                            'arguments': json.dumps({'name': 'hermes-agent-skill-authoring'}),
                        },
                    },
                    {
                        'id': 'c2',
                        'function': {
                            'name': 'skill_manage',
                            'arguments': json.dumps({
                                'action': 'create',
                                'name': 'ai-news-top10',
                                'content': '# Skill',
                            }),
                        },
                    },
                    {
                        'id': 'c3',
                        'function': {
                            'name': 'skill_view',
                            'arguments': json.dumps({'name': 'ai-news-top10'}),
                        },
                    },
                ],
            },
            {'role': 'tool', 'tool_call_id': 'c1', 'content': json.dumps({
                'success': True,
                'name': 'hermes-agent-skill-authoring',
                'path': str(skills_dir / 'hermes-agent-skill-authoring' / 'SKILL.md'),
            })},
            {
                'role': 'tool',
                'tool_call_id': 'c2',
                'content': json.dumps({'success': True, 'path': 'research/ai-news-top10'}),
            },
            {'role': 'tool', 'tool_call_id': 'c3', 'content': json.dumps({
                'success': True,
                'name': 'ai-news-top10',
                'path': str(skills_dir / 'research' / 'ai-news-top10' / 'SKILL.md'),
            })},
        ],
        tool_calls=[],
    )
    monkeypatch.setattr('api.session_manifest._load_display_messages', lambda s: list(s.messages))
    monkeypatch.setattr('api.session_manifest._skills_dir_for_session', lambda s: skills_dir)
    monkeypatch.setattr('api.session_manifest._skillhub_preview_available', lambda: True)

    manifest = build_session_manifest(session)

    artifact_paths = {row['path'] for row in manifest['artifacts']}
    reference_paths = {row['path'] for row in manifest['references']}
    assert 'research/ai-news-top10' in artifact_paths
    assert 'ai-news-top10' not in reference_paths
    assert 'research/ai-news-top10' not in reference_paths
    assert 'hermes-agent-skill-authoring' in reference_paths


def test_canonical_skill_path_normalization(tmp_path, monkeypatch):
    from api.session_manifest import _canonical_skill_manifest_path

    skills_dir = tmp_path / 'profile-home' / 'skills'
    _write_local_skill(skills_dir, 'ai-news-top10', rel_path='research/ai-news-top10')
    _write_local_skill(skills_dir, 'excalidraw', rel_path='creative/excalidraw')

    assert _canonical_skill_manifest_path('ai-news-top10', skills_dir) == 'research/ai-news-top10'
    assert _canonical_skill_manifest_path('research/ai-news-top10', skills_dir) == 'research/ai-news-top10'
    assert _canonical_skill_manifest_path('creative/excalidraw/SKILL.md', skills_dir) == 'creative/excalidraw'
    assert _canonical_skill_manifest_path(
        str(skills_dir / 'creative' / 'excalidraw' / 'SKILL.md'),
        skills_dir,
    ) == 'creative/excalidraw'
    assert _canonical_skill_manifest_path(
        'home/hermeswebui/.hermes/skills/creative/excalidraw/SKILL.md',
        skills_dir,
    ) == 'creative/excalidraw'


def test_canonical_manifest_file_key_normalizes_without_existence(tmp_path):
    from api.session_manifest import _canonical_manifest_file_key

    workspace = tmp_path / 'ws'
    workspace.mkdir()
    assert _canonical_manifest_file_key('`./notes.txt`', workspace) == 'notes.txt'
    assert _canonical_manifest_file_key('file://notes.txt', workspace) == 'notes.txt'
    assert _canonical_manifest_file_key(str(workspace / 'gone.excalidraw'), workspace) == 'gone.excalidraw'
    external = tmp_path / 'outside' / 'report.md'
    assert _canonical_manifest_file_key(str(external), workspace) == external.resolve().as_posix()


def test_build_session_manifest_drops_reference_when_same_file_is_artifact(tmp_path, monkeypatch):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    target = workspace / 'chart.excalidraw'
    target.write_text('{}', encoding='utf-8')
    session = Session(
        session_id='manifestdedupefile01',
        workspace=str(workspace),
        messages=[
            {'role': 'user', 'content': 'draw chart', '_turn_key': 'turn:1'},
            {
                'role': 'assistant',
                'tool_calls': [
                    {
                        'id': 'c1',
                        'function': {
                            'name': 'write_file',
                            'arguments': json.dumps({'path': 'chart.excalidraw'}),
                        },
                    },
                    {
                        'id': 'c2',
                        'function': {
                            'name': 'read_file',
                            'arguments': json.dumps({'path': 'chart.excalidraw'}),
                        },
                    },
                ],
            },
            {'role': 'tool', 'tool_call_id': 'c1', 'content': 'ok'},
            {'role': 'tool', 'tool_call_id': 'c2', 'content': '{}'},
        ],
        tool_calls=[],
    )
    monkeypatch.setattr('api.session_manifest._load_display_messages', lambda s: list(s.messages))

    manifest = build_session_manifest(session)

    assert manifest['artifacts'] == [{
        'path': 'chart.excalidraw',
        'preview': MANIFEST_PREVIEW_FILE,
        'source_tool': 'write_file',
    }]
    assert manifest['references'] == []
    assert manifest['turns'][0]['references'] == []


def test_build_session_manifest_drops_persisted_artifact_file_from_references(tmp_path, monkeypatch):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    target = workspace / 'chart.excalidraw'
    target.write_text('{}', encoding='utf-8')
    session = Session(
        session_id='manifestdedupefile02',
        workspace=str(workspace),
        profile='default',
        messages=[
            {'role': 'user', 'content': 'verify chart', '_turn_key': 'turn:1'},
            {
                'role': 'assistant',
                'tool_calls': [{
                    'id': 'c1',
                    'function': {
                        'name': 'read_file',
                        'arguments': json.dumps({'path': 'chart.excalidraw'}),
                    },
                }],
            },
            {'role': 'tool', 'tool_call_id': 'c1', 'content': '{}'},
        ],
        tool_calls=[],
    )
    monkeypatch.setattr('api.session_manifest._load_display_messages', lambda s: list(s.messages))
    monkeypatch.setattr(
        'api.session_manifest_store.load_manifest_records',
        lambda s, include_lineage=True: [{
            'session_id': s.session_id,
            'lineage_key': s.session_id,
            'profile': 'default',
            'turn_key': 'turn:1',
            'record_kind': 'artifact',
            'path': 'chart.excalidraw',
            'preview': 'file',
            'source_tool': 'assistant_prose',
        }],
    )
    monkeypatch.setattr(
        'api.session_manifest_store.load_manifest_decided_turn_keys',
        lambda *args, **kwargs: {'turn:1'},
    )

    manifest = build_session_manifest(session)

    assert manifest['artifacts'] == [{
        'path': 'chart.excalidraw',
        'preview': MANIFEST_PREVIEW_FILE,
        'source_tool': 'assistant_prose',
        'profile': 'default',
    }]
    assert manifest['references'] == []
    assert manifest['turns'][0]['references'] == []


def test_skill_view_failed_with_warning_does_not_create_reference(tmp_path, monkeypatch):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    skills_dir = tmp_path / 'profile-home' / 'skills'
    _write_local_skill(skills_dir, 'excalidraw', rel_path='creative/excalidraw')
    _write_local_skill(skills_dir, 'excalidraw', rel_path='excalidraw')
    session = Session(
        session_id='manifestskillwarn01',
        workspace=str(workspace),
        messages=[
            {'role': 'user', 'content': 'open skill', '_turn_key': 'turn:1'},
            {
                'role': 'assistant',
                'tool_calls': [{
                    'id': 'c1',
                    'function': {
                        'name': 'skill_view',
                        'arguments': json.dumps({'name': 'excalidraw'}),
                    },
                }],
            },
            {
                'role': 'tool',
                'tool_call_id': 'c1',
                'content': (
                    json.dumps({
                        'success': False,
                        'error': "Ambiguous skill name 'excalidraw': 2 skills match",
                    })
                    + '\n[Tool loop warning: repeated similar calls]'
                ),
            },
        ],
        tool_calls=[],
    )
    monkeypatch.setattr('api.session_manifest._load_display_messages', lambda s: list(s.messages))
    monkeypatch.setattr('api.session_manifest._skills_dir_for_session', lambda s: skills_dir)
    monkeypatch.setattr('api.session_manifest._skillhub_preview_available', lambda: True)

    manifest = build_session_manifest(session)

    assert manifest['references'] == []
    assert manifest['turns'][0]['references'] == []


def test_skill_view_success_keeps_canonical_skill_reference(tmp_path, monkeypatch):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    skills_dir = tmp_path / 'profile-home' / 'skills'
    _write_local_skill(skills_dir, 'excalidraw', rel_path='creative/excalidraw')
    session = Session(
        session_id='manifestskillsuccess01',
        workspace=str(workspace),
        messages=[
            {'role': 'user', 'content': 'open skill', '_turn_key': 'turn:1'},
            {
                'role': 'assistant',
                'tool_calls': [{
                    'id': 'c1',
                    'function': {
                        'name': 'skill_view',
                        'arguments': json.dumps({'name': 'creative/excalidraw'}),
                    },
                }],
            },
            {
                'role': 'tool',
                'tool_call_id': 'c1',
                'content': json.dumps({
                    'success': True,
                    'name': 'excalidraw',
                    'path': str(skills_dir / 'creative' / 'excalidraw' / 'SKILL.md'),
                }),
            },
        ],
        tool_calls=[],
    )
    monkeypatch.setattr('api.session_manifest._load_display_messages', lambda s: list(s.messages))
    monkeypatch.setattr('api.session_manifest._skills_dir_for_session', lambda s: skills_dir)
    monkeypatch.setattr('api.session_manifest._skillhub_preview_available', lambda: True)

    manifest = build_session_manifest(session)

    assert manifest['references'] == [{
        'path': 'creative/excalidraw',
        'preview': MANIFEST_PREVIEW_SKILL,
        'source_tool': 'skill_view',
    }]


def test_skill_view_dedupes_bare_and_skill_md_paths_to_one_canonical_reference(tmp_path, monkeypatch):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    skills_dir = tmp_path / 'profile-home' / 'skills'
    _write_local_skill(skills_dir, 'excalidraw', rel_path='creative/excalidraw')
    _write_local_skill(skills_dir, 'excalidraw', rel_path='excalidraw')
    session = Session(
        session_id='manifestskilldedupe01',
        workspace=str(workspace),
        messages=[
            {'role': 'user', 'content': 'open skill', '_turn_key': 'turn:1'},
            {
                'role': 'assistant',
                'tool_calls': [
                    {
                        'id': 'c1',
                        'function': {
                            'name': 'skill_view',
                            'arguments': json.dumps({'name': 'excalidraw'}),
                        },
                    },
                    {
                        'id': 'c2',
                        'function': {
                            'name': 'skill_view',
                            'arguments': json.dumps({'name': 'creative/excalidraw'}),
                        },
                    },
                    {
                        'id': 'c3',
                        'function': {
                            'name': 'skill_view',
                            'arguments': json.dumps({
                                'name': 'home/hermeswebui/.hermes/skills/excalidraw/SKILL.md',
                            }),
                        },
                    },
                ],
            },
            {
                'role': 'tool',
                'tool_call_id': 'c1',
                'content': (
                    json.dumps({
                        'success': False,
                        'error': "Ambiguous skill name 'excalidraw': 2 skills match",
                    })
                    + '\n[Tool loop warning: repeated similar calls]'
                ),
            },
            {
                'role': 'tool',
                'tool_call_id': 'c2',
                'content': json.dumps({
                    'success': True,
                    'name': 'excalidraw',
                    'path': str(skills_dir / 'creative' / 'excalidraw' / 'SKILL.md'),
                }),
            },
            {
                'role': 'tool',
                'tool_call_id': 'c3',
                'content': (
                    json.dumps({
                        'success': False,
                        'error': 'skill not found',
                    })
                    + '\n[Tool loop warning: repeated similar calls]'
                ),
            },
        ],
        tool_calls=[],
    )
    monkeypatch.setattr('api.session_manifest._load_display_messages', lambda s: list(s.messages))
    monkeypatch.setattr('api.session_manifest._skills_dir_for_session', lambda s: skills_dir)
    monkeypatch.setattr('api.session_manifest._skillhub_preview_available', lambda: True)

    manifest = build_session_manifest(session)

    assert manifest['references'] == [{
        'path': 'creative/excalidraw',
        'preview': MANIFEST_PREVIEW_SKILL,
        'source_tool': 'skill_view',
    }]
    assert all('SKILL.md' not in row['path'] for row in manifest['references'])
    assert all(row['path'] != 'excalidraw' for row in manifest['references'])

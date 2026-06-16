"""Tests for session manifest extraction (todos, artifacts, references)."""

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
    _message_turns,
    _next_turn_key,
    _normalize_manifest_path,
    _paths_from_assistant_media,
    _paths_from_assistant_prose,
    _public_todo_items,
    _resolve_manifest_path,
    _rows_to_wire,
    _serialize_manifest_row,
    _wire_todos,
    build_session_manifest,
    extract_manifest_delta_from_assistant_media,
    extract_manifest_delta_from_turn_reconcile,
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
    events = [
        ToolEvent(name='skill_view', args={'name': 'my-skill'}, assistant_msg_idx=1),
    ]
    monkeypatch.setattr('api.session_manifest._skillhub_preview_available', lambda: True)

    _artifacts, references = _extract_artifacts_and_references(events, workspace)
    wire = _rows_to_wire(references, workspace)

    assert wire == [{
        'path': 'my-skill',
        'preview': MANIFEST_PREVIEW_SKILL,
        'source_tool': 'skill_view',
    }]


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
    wire = _rows_to_wire(artifacts, workspace, skills_dir)

    assert references == []
    assert wire == [{
        'path': 'foo',
        'preview': MANIFEST_PREVIEW_SKILL,
        'source_tool': 'skill_manage',
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
    assert write_delta['artifacts'] == [{
        'path': 'notes.txt',
        'preview': 'file',
        'source_tool': 'write_file',
    }]


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
    assert len(manifest['references']) == 0


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


def test_paths_from_assistant_prose_labeled_unicode_path(tmp_path):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    docx = workspace / 'AI热点top10-2026-06.docx'
    docx.write_bytes(b'fake-docx')
    labeled = f'📄 文件位置：{docx}'
    paths = _paths_from_assistant_prose(labeled, workspace)
    assert paths == ['AI热点top10-2026-06.docx']


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
    assert manifest['artifacts'] == []
    assert manifest['turns'][0]['artifacts'] == []


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


def test_build_session_manifest_relative_path_without_delivery_context(tmp_path, monkeypatch):
    """相对路径（无交付关键词，不提及绝对路径）→ 不出 artifacts"""
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
    assert not any(
        row['path'] == 'src/main.py' and row['source_tool'] == ASSISTANT_PROSE_ARTIFACT_SOURCE
        for row in manifest['artifacts']
    )


def test_build_session_manifest_code_span_path_without_delivery_context(tmp_path, monkeypatch):
    """代码块裸文件名（反引号，无绝对路径）→ 不出 artifacts"""
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
    assert not any(
        row['path'] == 'result.json' and row['source_tool'] == ASSISTANT_PROSE_ARTIFACT_SOURCE
        for row in manifest['artifacts']
    )


def test_build_session_manifest_basename_no_delivery_context_not_artifact(tmp_path, monkeypatch):
    """裸基名（无路径分隔符）在 assistant 正文中，无交付关键词，文件存在 → 不出 artifacts"""
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
    assert manifest['artifacts'] == []


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


# ── 改动二：read_file 等读取类工具不再产出 references ──


def test_build_session_manifest_read_file_no_reference_nor_artifact(tmp_path, monkeypatch):
    """read_file 读取存在的文件 → 不出现 references 和 artifacts"""
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    (workspace / 'config.yaml').write_text('key: val', encoding='utf-8')
    sid = 'read_file_no_ref01'
    session = Session(
        session_id=sid,
        workspace=str(workspace),
        messages=[
            {'role': 'user', 'content': '读取配置'},
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


def test_ensure_turn_keys_stamps_missing():
    """_ensure_turn_keys() 给缺失 _turn_key 的用户消息打戳"""
    messages = [
        {'role': 'user', 'content': 'q1', '_turn_key': 'turn:5'},
        {'role': 'assistant', 'content': 'a1'},
        {'role': 'user', 'content': 'q2'},  # 缺失
        {'role': 'user', 'content': 'q3', '_turn_key': 'turn:7'},
        {'role': 'user', 'content': 'q4'},  # 缺失
    ]
    _ensure_turn_keys(messages)
    assert messages[0]['_turn_key'] == 'turn:5'  # 已有，不变
    assert messages[2]['_turn_key'] == 'turn:8'  # max(5,7)+1=8
    assert messages[3]['_turn_key'] == 'turn:7'  # 已有，不变
    assert messages[4]['_turn_key'] == 'turn:9'  # 下一个


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

    assert len(manifest['turns']) == 3  # 第一轮 + 压缩标记 + 第二轮
    # 第一轮的 turn key 稳定
    assert manifest['turns'][0]['turn_key'] == 'turn:1'
    # 第二轮的 turn key 稳定（不因为中间插入标记而偏移）
    assert manifest['turns'][2]['turn_key'] == 'turn:2'
    # 第一轮有写入的成果
    assert len(manifest['turns'][0]['artifacts']) >= 1
    assert manifest['turns'][0]['artifacts'][0]['path'] == 'first.txt'

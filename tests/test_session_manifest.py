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
    ToolEvent,
    _apply_public_todos_to_manifest_delta,
    _collect_media_artifact_events,
    _collect_tool_events,
    _extract_artifacts_and_references,
    _extract_latest_todos,
    _extract_manifest_records,
    _normalize_manifest_path,
    _paths_from_assistant_media,
    _public_todo_items,
    _resolve_manifest_path,
    _rows_to_wire,
    _serialize_manifest_row,
    _wire_todos,
    build_session_manifest,
    extract_manifest_delta_from_assistant_media,
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


def test_serialize_manifest_row_shape():
    row = _serialize_manifest_row('docs/a.md', MANIFEST_PREVIEW_FILE, 'read_file')
    assert row == {
        'path': 'docs/a.md',
        'preview': 'file',
        'source_tool': 'read_file',
    }


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
    assert len(manifest['references']) == 1
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
    assert delta['source']['tool'] == MEDIA_ARTIFACT_SOURCE
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

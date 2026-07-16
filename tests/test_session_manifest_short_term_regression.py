import json

from api.models import Session
from api.session_manifest import (
    MANIFEST_PREVIEW_FILE,
    ToolEvent,
    _collect_turn_artifact_entries_from_events,
    _terminal_output_paths,
    build_session_manifest,
    extract_turn_artifact_entries_for_manifest,
)


def _manifest_without_store(monkeypatch, session):
    monkeypatch.setattr('api.session_manifest._load_display_messages', lambda _s: list(session.messages))
    monkeypatch.setattr('api.session_manifest_store.load_manifest_records', lambda *args, **kwargs: [])
    monkeypatch.setattr('api.session_manifest_store.load_manifest_decided_turn_keys', lambda *args, **kwargs: set())
    monkeypatch.setattr('api.session_manifest_store.repair_empty_manifest_turns', lambda *args, **kwargs: 0)
    monkeypatch.setattr('api.session_manifest_store.backfill_missing_manifest_records', lambda *args, **kwargs: {'source': 'derived'})


def test_strong_subdirectory_path_wins_over_root_basename(monkeypatch, tmp_path):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    (workspace / 'report.md').write_text('old', encoding='utf-8')
    nested = workspace / 'deliveries' / 'report.md'
    nested.parent.mkdir()
    nested.write_text('new', encoding='utf-8')
    session = Session(
        session_id='strongbasename01',
        workspace=str(workspace),
        messages=[
            {'role': 'user', 'content': 'write it', '_turn_key': 'turn:1'},
            {'role': 'assistant', 'tool_calls': [{
                'id': 'write-1',
                'function': {'name': 'write_file', 'arguments': json.dumps({'path': 'deliveries/report.md'})},
            }]},
            {'role': 'tool', 'tool_call_id': 'write-1', 'content': 'ok'},
            {'role': 'assistant', 'content': '交付 `report.md`。'},
        ],
        tool_calls=[],
    )
    _manifest_without_store(monkeypatch, session)

    manifest = build_session_manifest(session)

    assert manifest['turns'][0]['artifacts'] == [{
        'path': 'deliveries/report.md',
        'preview': MANIFEST_PREVIEW_FILE,
        'source_tool': 'write_file',
    }]


def test_followup_prose_resolves_unique_prior_artifact(monkeypatch, tmp_path):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    (workspace / 'report.md').write_text('old', encoding='utf-8')
    nested = workspace / 'deliveries' / 'report.md'
    nested.parent.mkdir()
    nested.write_text('new', encoding='utf-8')
    session = Session(
        session_id='priorbasename01',
        workspace=str(workspace),
        messages=[
            {'role': 'user', 'content': 'write it', '_turn_key': 'turn:1'},
            {'role': 'assistant', 'tool_calls': [{
                'id': 'write-1',
                'function': {'name': 'write_file', 'arguments': json.dumps({'path': 'deliveries/report.md'})},
            }]},
            {'role': 'tool', 'tool_call_id': 'write-1', 'content': 'ok'},
            {'role': 'assistant', 'content': '已生成 `report.md`。'},
            {'role': 'user', 'content': '哪些文件生成了？', '_turn_key': 'turn:4'},
            {'role': 'assistant', 'content': '本次交付 `report.md`。'},
        ],
        tool_calls=[],
    )
    _manifest_without_store(monkeypatch, session)

    manifest = build_session_manifest(session)
    by_turn = {turn['turn_key']: turn['artifacts'] for turn in manifest['turns']}

    assert by_turn['turn:1'][0]['path'] == 'deliveries/report.md'
    assert by_turn['turn:4'] == [{
        'path': 'deliveries/report.md',
        'preview': MANIFEST_PREVIEW_FILE,
        'source_tool': 'assistant_prose',
    }]


def test_ambiguous_prior_basename_is_not_resolved(monkeypatch, tmp_path):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    for directory in ('first', 'second'):
        target = workspace / directory / 'report.md'
        target.parent.mkdir(exist_ok=True)
        target.write_text(directory, encoding='utf-8')
    session = Session(
        session_id='ambiguousbasename01',
        workspace=str(workspace),
        messages=[
            {'role': 'user', 'content': 'first', '_turn_key': 'turn:1'},
            {'role': 'assistant', 'tool_calls': [{
                'id': 'write-1',
                'function': {'name': 'write_file', 'arguments': json.dumps({'path': 'first/report.md'})},
            }]},
            {'role': 'tool', 'tool_call_id': 'write-1', 'content': 'ok'},
            {'role': 'assistant', 'content': 'done'},
            {'role': 'user', 'content': 'second', '_turn_key': 'turn:4'},
            {'role': 'assistant', 'tool_calls': [{
                'id': 'write-2',
                'function': {'name': 'write_file', 'arguments': json.dumps({'path': 'second/report.md'})},
            }]},
            {'role': 'tool', 'tool_call_id': 'write-2', 'content': 'ok'},
            {'role': 'assistant', 'content': 'done'},
            {'role': 'user', 'content': 'list', '_turn_key': 'turn:8'},
            {'role': 'assistant', 'content': '交付 `report.md`。'},
        ],
        tool_calls=[],
    )
    _manifest_without_store(monkeypatch, session)

    manifest = build_session_manifest(session)
    by_turn = {turn['turn_key']: turn['artifacts'] for turn in manifest['turns']}

    assert by_turn['turn:8'] == []


def test_failed_mutation_and_skill_manage_do_not_persist(tmp_path):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    (workspace / 'report.md').write_text('existing', encoding='utf-8')
    events = [
        ToolEvent(
            name='write_file',
            args={'path': 'report.md'},
            result=json.dumps({'success': False}),
            status='completed',
        ),
        ToolEvent(
            name='skill_manage',
            args={'action': 'create', 'name': 'bad-skill'},
            result=json.dumps({'success': False}),
            status='completed',
        ),
    ]

    assert _collect_turn_artifact_entries_from_events(events, workspace) == []


def test_session_tool_call_requires_explicit_completion(tmp_path):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    target = workspace / 'report.md'
    target.write_text('content', encoding='utf-8')
    session = Session(
        session_id='sessiontoolcall01',
        workspace=str(workspace),
        messages=[{'role': 'user', 'content': 'write', '_turn_key': 'turn:1'}],
        tool_calls=[{
            'name': 'write_file',
            'args': {'path': 'report.md'},
            'assistant_msg_idx': 0,
        }],
    )

    assert extract_turn_artifact_entries_for_manifest(session, 'turn:1') == []


def test_terminal_md2word_positional_output_requires_real_file(tmp_path):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    delivery = workspace / 'deliveries'
    delivery.mkdir()
    output = delivery / 'report.docx'
    output.write_bytes(b'docx')
    command = 'cd deliveries && python3 /tools/md2word.py input.md report.docx --preset=report'

    assert _terminal_output_paths(command, workspace) == ['deliveries/report.docx']
    output.unlink()
    assert _terminal_output_paths(command, workspace) == []

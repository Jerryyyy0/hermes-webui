from api.models import Session
from integration.session_manifest.repair import repair_incomplete_manifest_turn
from integration.session_manifest.store import load_manifest_records, upsert_manifest_records


def _session(workspace, messages):
    return Session(
        session_id='repairturn001',
        workspace=str(workspace),
        messages=messages,
        tool_calls=[],
    )


def test_repair_incomplete_turn_is_dry_run_then_additive_and_idempotent(tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    (workspace / 'brief.md').write_text('brief', encoding='utf-8')
    (workspace / 'proposal.html').write_text('<h1>proposal</h1>', encoding='utf-8')
    db_path = tmp_path / 'state' / 'session_manifest.db'
    session = _session(workspace, [
        {'role': 'user', 'content': 'prepare proposal', '_turn_key': 'turn:1'},
        {
            'role': 'assistant',
            'tool_calls': [{
                'id': 'write-proposal',
                'function': {
                    'name': 'write_file',
                    'arguments': '{"path":"proposal.html"}',
                },
            }],
        },
        {'role': 'tool', 'tool_call_id': 'write-proposal', 'content': 'saved'},
        {'role': 'assistant', 'content': f'MEDIA:{workspace / "proposal.html"}'},
        {'role': 'user', 'content': 'where is it?', '_turn_key': 'turn:2'},
        {'role': 'assistant', 'content': f'MEDIA:{workspace / "proposal.html"}'},
    ])
    upsert_manifest_records(
        session,
        'turn:1',
        [{'path': 'brief.md', 'source_tool': 'write_file'}],
        db_path=db_path,
    )

    preview = repair_incomplete_manifest_turn(session, 'turn:1', db_path=db_path)

    assert preview['status'] == 'dry_run'
    assert preview['missing_records'] == [{
        'path': 'proposal.html',
        'preview': 'file',
        'source_tool': 'write_file',
    }]
    assert [row['path'] for row in load_manifest_records(session, db_path=db_path)] == ['brief.md']

    applied = repair_incomplete_manifest_turn(session, 'turn:1', apply=True, db_path=db_path)

    assert applied['status'] == 'applied'
    assert [row['path'] for row in load_manifest_records(session, db_path=db_path)] == [
        'brief.md', 'proposal.html',
    ]
    assert repair_incomplete_manifest_turn(session, 'turn:1', apply=True, db_path=db_path)['status'] == 'no_change'


def test_repair_incomplete_turn_rejects_paths_modified_by_a_later_turn(tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    (workspace / 'brief.md').write_text('brief', encoding='utf-8')
    (workspace / 'proposal.html').write_text('<h1>newer</h1>', encoding='utf-8')
    db_path = tmp_path / 'state' / 'session_manifest.db'
    session = _session(workspace, [
        {'role': 'user', 'content': 'prepare proposal', '_turn_key': 'turn:1'},
        {'role': 'assistant', 'content': f'MEDIA:{workspace / "proposal.html"}'},
        {'role': 'user', 'content': 'revise proposal', '_turn_key': 'turn:2'},
        {
            'role': 'assistant',
            'tool_calls': [{
                'id': 'rewrite-proposal',
                'function': {
                    'name': 'write_file',
                    'arguments': '{"path":"proposal.html"}',
                },
            }],
        },
        {'role': 'tool', 'tool_call_id': 'rewrite-proposal', 'content': 'saved'},
    ])
    upsert_manifest_records(
        session,
        'turn:1',
        [{'path': 'brief.md', 'source_tool': 'write_file'}],
        db_path=db_path,
    )

    report = repair_incomplete_manifest_turn(session, 'turn:1', apply=True, db_path=db_path)

    assert report['status'] == 'version_conflict'
    assert report['conflicting_paths'] == ['proposal.html']
    assert [row['path'] for row in load_manifest_records(session, db_path=db_path)] == ['brief.md']


def test_repair_incomplete_turn_fails_closed_for_a_truncated_transcript(tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    session = _session(workspace, [
        {'role': 'user', 'content': 'prepare proposal', '_turn_key': 'turn:1'},
    ])
    session._messages_truncated = True

    report = repair_incomplete_manifest_turn(session, 'turn:1', apply=True)

    assert report['status'] == 'transcript_incomplete'

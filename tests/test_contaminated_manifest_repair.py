from api.models import Session
from integration.session_manifest.repair import repair_contaminated_manifest_session
from integration.session_manifest.store import (
    load_manifest_empty_turn_keys,
    load_manifest_records,
    upsert_manifest_records,
)


def _repair_session(workspace):
    return Session(
        session_id='contaminated-session',
        workspace=str(workspace),
        profile='ops',
        messages=[
            {'role': 'user', 'content': 'write old', '_turn_key': 'turn:1'},
            {'role': 'assistant', 'content': ''},
            {'role': 'user', 'content': 'write current', '_turn_key': 'turn:2'},
            {'role': 'assistant', 'content': ''},
            {'role': 'user', 'content': 'inspect only', '_turn_key': 'turn:3'},
            {'role': 'assistant', 'content': 'No new file.'},
        ],
        tool_calls=[
            {
                'name': 'write_file',
                'args': {'path': 'old.md'},
                'assistant_msg_idx': 1,
                'tid': 'write-old',
                'done': True,
                'snippet': '{"bytes_written": 3}',
            },
            {
                'name': 'write_file',
                'args': {'path': 'current.md'},
                'assistant_msg_idx': 3,
                'tid': 'write-current',
                'done': True,
                'snippet': '{"bytes_written": 7}',
            },
        ],
    )


def _seed_contamination(session, db_path):
    upsert_manifest_records(
        session,
        'turn:1',
        [{'path': 'old.md', 'source_tool': 'write_file'}],
        db_path=db_path,
    )
    upsert_manifest_records(
        session,
        'turn:2',
        [{'path': 'old.md', 'source_tool': 'write_file'}],
        db_path=db_path,
    )
    upsert_manifest_records(
        session,
        'turn:3',
        [
            {'path': '', 'source_tool': 'assistant_prose'},
            {'path': 'old.md', 'source_tool': 'write_file'},
            {'path': 'current.md', 'source_tool': 'write_file'},
        ],
        db_path=db_path,
    )


def test_contaminated_session_repair_is_dry_run_atomic_and_idempotent(tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    (workspace / 'old.md').write_text('old', encoding='utf-8')
    (workspace / 'current.md').write_text('current', encoding='utf-8')
    db_path = tmp_path / 'state' / 'session_manifest.db'
    backup_path = tmp_path / 'backup' / 'session_manifest.db'
    session = _repair_session(workspace)
    _seed_contamination(session, db_path)

    preview = repair_contaminated_manifest_session(session, db_path=db_path)

    assert preview['status'] == 'dry_run'
    assert preview['summary'] == {'kept': 2, 'added': 1, 'removed': 3}
    assert not backup_path.exists()
    assert [(row['turn_key'], row['path']) for row in load_manifest_records(session, db_path=db_path)] == [
        ('turn:1', 'old.md'),
        ('turn:2', 'old.md'),
        ('turn:3', 'current.md'),
        ('turn:3', 'old.md'),
    ]
    assert load_manifest_empty_turn_keys(session, db_path=db_path) == set()

    applied = repair_contaminated_manifest_session(
        session,
        apply=True,
        db_path=db_path,
        backup_path=backup_path,
    )

    assert applied['status'] == 'applied'
    assert applied['summary'] == preview['summary']
    assert backup_path.is_file()
    assert [(row['turn_key'], row['path']) for row in load_manifest_records(session, db_path=backup_path)] == [
        ('turn:1', 'old.md'),
        ('turn:2', 'old.md'),
        ('turn:3', 'current.md'),
        ('turn:3', 'old.md'),
    ]
    assert [(row['turn_key'], row['path']) for row in load_manifest_records(session, db_path=db_path)] == [
        ('turn:1', 'old.md'),
        ('turn:2', 'current.md'),
    ]
    assert load_manifest_empty_turn_keys(session, db_path=db_path) == {'turn:3'}
    assert repair_contaminated_manifest_session(session, db_path=db_path)['status'] == 'no_change'


def test_contaminated_session_repair_fails_closed_for_incomplete_transcript(tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    session = _repair_session(workspace)
    session._messages_truncated = True

    report = repair_contaminated_manifest_session(
        session,
        apply=True,
        db_path=tmp_path / 'missing.db',
    )

    assert report['status'] == 'transcript_incomplete'


def test_contaminated_session_repair_refuses_a_shared_lineage(tmp_path, monkeypatch):
    from integration.session_manifest import store

    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    session = _repair_session(workspace)
    monkeypatch.setattr(store, 'resolve_manifest_lineage_key', lambda _session: 'parent-session')

    report = repair_contaminated_manifest_session(
        session,
        apply=True,
        db_path=tmp_path / 'missing.db',
    )

    assert report['status'] == 'shared_lineage_unsupported'

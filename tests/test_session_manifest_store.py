import json
from types import SimpleNamespace

from api import session_manifest_store as store


def _session(session_id, *, profile='ops', parent_session_id=None, pre_compression_snapshot=False):
    return SimpleNamespace(
        session_id=session_id,
        profile=profile,
        parent_session_id=parent_session_id,
        pre_compression_snapshot=pre_compression_snapshot,
    )


def test_store_upsert_uses_session_profile_and_normalizes_source_tool(tmp_path):
    db_path = tmp_path / 'manifest.db'
    session = _session('storeprof01', profile='ops')

    rows = store.upsert_manifest_records(
        session,
        'turn:1',
        [{
            'path': 'report.md',
            'source_tool': '',
            'profile': 'ignored',
        }],
        db_path=db_path,
    )

    assert rows[0]['profile'] == 'ops'
    assert rows[0]['source_tool'] == 'assistant_prose'
    loaded = store.load_manifest_records(session, db_path=db_path)
    assert loaded[0]['profile'] == 'ops'
    assert loaded[0]['source_tool'] == 'assistant_prose'


def test_store_missing_profile_is_empty_string(tmp_path):
    db_path = tmp_path / 'manifest.db'
    session = _session('storeemptyprof01', profile=None)

    store.upsert_manifest_records(
        session,
        'turn:1',
        [{'path': 'report.md', 'source_tool': 'write_file'}],
        db_path=db_path,
    )

    loaded = store.load_manifest_records(session, db_path=db_path)
    assert loaded[0]['profile'] == ''


def test_store_keeps_same_path_separate_by_profile(tmp_path):
    db_path = tmp_path / 'manifest.db'
    store.upsert_manifest_records(
        _session('storesamepath01', profile='ops'),
        'turn:1',
        [{'path': 'report.md', 'source_tool': 'write_file'}],
        db_path=db_path,
    )
    store.upsert_manifest_records(
        _session('storesamepath02', profile='research'),
        'turn:1',
        [{'path': 'report.md', 'source_tool': 'write_file'}],
        db_path=db_path,
    )

    assert len(store.load_manifest_records(_session('storesamepath01', profile='ops'), db_path=db_path)) == 1
    assert len(store.load_manifest_records(_session('storesamepath02', profile='research'), db_path=db_path)) == 1


def test_compression_child_reads_parent_lineage_records(tmp_path, monkeypatch):
    db_path = tmp_path / 'manifest.db'
    session_dir = tmp_path / 'sessions'
    session_dir.mkdir()
    (session_dir / 'parent01.json').write_text(
        '{"session_id":"parent01","pre_compression_snapshot":true,"messages":[]}',
        encoding='utf-8',
    )
    (session_dir / 'child01.json').write_text(
        '{"session_id":"child01","parent_session_id":"parent01","messages":[]}',
        encoding='utf-8',
    )
    monkeypatch.setattr(store, 'SESSION_DIR', session_dir)

    store.upsert_manifest_records(
        _session('parent01', profile='ops', pre_compression_snapshot=True),
        'turn:1',
        [{'path': 'report.md', 'source_tool': 'write_file'}],
        db_path=db_path,
    )

    loaded = store.load_manifest_records(
        _session('child01', profile='ops', parent_session_id='parent01'),
        db_path=db_path,
    )
    assert [row['path'] for row in loaded] == ['report.md']


def test_plain_fork_does_not_read_parent_records(tmp_path, monkeypatch):
    db_path = tmp_path / 'manifest.db'
    session_dir = tmp_path / 'sessions'
    session_dir.mkdir()
    (session_dir / 'parentfork01.json').write_text(
        '{"session_id":"parentfork01","pre_compression_snapshot":false,"messages":[]}',
        encoding='utf-8',
    )
    (session_dir / 'childfork01.json').write_text(
        '{"session_id":"childfork01","parent_session_id":"parentfork01","messages":[]}',
        encoding='utf-8',
    )
    monkeypatch.setattr(store, 'SESSION_DIR', session_dir)

    store.upsert_manifest_records(
        _session('parentfork01', profile='ops'),
        'turn:1',
        [{'path': 'report.md', 'source_tool': 'write_file'}],
        db_path=db_path,
    )

    loaded = store.load_manifest_records(
        _session('childfork01', profile='ops', parent_session_id='parentfork01'),
        db_path=db_path,
    )
    assert loaded == []


def test_delete_session_manifest_turns_prunes_invisible_turns(tmp_path):
    db_path = tmp_path / 'manifest.db'
    session = _session('storeprune01', profile='ops')
    store.upsert_manifest_records(
        session,
        'turn:1',
        [{'path': 'one.md', 'source_tool': 'write_file'}],
        db_path=db_path,
    )
    store.upsert_manifest_records(
        session,
        'turn:2',
        [{'path': 'two.md', 'source_tool': 'write_file'}],
        db_path=db_path,
    )

    store.delete_session_manifest_turns('storeprune01', {'turn:1'}, db_path=db_path)

    loaded = store.load_manifest_records(session, db_path=db_path)
    assert [row['path'] for row in loaded] == ['one.md']


def _write_session_sidecar(session_dir, sid, *, profile=None):
    session_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        'session_id': sid,
        'title': 'Test',
        'created_at': 1.0,
        'updated_at': 2.0,
        'profile': profile,
        'messages': [],
    }
    (session_dir / f'{sid}.json').write_text(
        json.dumps(payload),
        encoding='utf-8',
    )


def test_backfill_empty_profile_patches_rows_with_session_profile(tmp_path, monkeypatch):
    db_path = tmp_path / 'manifest.db'
    session_dir = tmp_path / 'sessions'
    sid = 'backfill01'
    monkeypatch.setattr('api.models.SESSION_DIR', session_dir)
    monkeypatch.setattr('api.config.SESSION_DIR', session_dir)

    store.upsert_manifest_records(
        _session(sid, profile=None),
        'turn:1',
        [{'path': 'one.md', 'source_tool': 'write_file'}],
        db_path=db_path,
    )
    store.upsert_manifest_records(
        _session(sid, profile=None),
        'turn:2',
        [{'path': 'two.md', 'source_tool': 'write_file'}],
        db_path=db_path,
    )
    _write_session_sidecar(session_dir, sid, profile='ops')

    result = store.backfill_empty_profile_artifacts(db_path=db_path)

    assert result == {'scanned': 1, 'patched': 2, 'skipped': 0}
    loaded = store.load_manifest_records(_session(sid, profile='ops'), db_path=db_path)
    assert len(loaded) == 2
    assert all(row['profile'] == 'ops' for row in loaded)


def test_backfill_empty_profile_skips_when_session_profile_also_empty(tmp_path, monkeypatch):
    db_path = tmp_path / 'manifest.db'
    session_dir = tmp_path / 'sessions'
    sid = 'backfill02'
    monkeypatch.setattr('api.models.SESSION_DIR', session_dir)
    monkeypatch.setattr('api.config.SESSION_DIR', session_dir)

    store.upsert_manifest_records(
        _session(sid, profile=None),
        'turn:1',
        [{'path': 'one.md', 'source_tool': 'write_file'}],
        db_path=db_path,
    )
    _write_session_sidecar(session_dir, sid, profile=None)

    result = store.backfill_empty_profile_artifacts(db_path=db_path)

    assert result == {'scanned': 1, 'patched': 0, 'skipped': 1}
    loaded = store.load_manifest_records(_session(sid, profile=None), db_path=db_path)
    assert loaded[0]['profile'] == ''


def _bclass_session(sid, *, workspace, profile='ops', messages=None, turn_artifacts=None):
    """A session-like object with the attributes backfill_session_artifacts reads."""
    return SimpleNamespace(
        session_id=sid,
        profile=profile,
        parent_session_id=None,
        pre_compression_snapshot=False,
        workspace=str(workspace),
        messages=messages or [],
        tool_calls=[],
        turn_artifacts=turn_artifacts,
    )


def _write_file_turn_messages(turn_key, filename):
    """Build a user+assistant(tool_use)+tool(result) turn that writes a file."""
    return [
        {'role': 'user', 'content': 'write a report', '_turn_key': turn_key},
        {
            'role': 'assistant',
            'content': [
                {
                    'type': 'tool_use',
                    'id': 'toolu_1',
                    'name': 'write_file',
                    'input': {'path': filename, 'content': 'hello'},
                },
            ],
        },
        {
            'role': 'tool',
            'tool_call_id': 'toolu_1',
            'content': f'wrote {filename}',
        },
    ]


def test_backfill_session_artifacts_extracts_from_messages_without_turn_artifacts(tmp_path):
    db_path = tmp_path / 'manifest.db'
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    (workspace / 'report.md').write_text('hello', encoding='utf-8')

    messages = _write_file_turn_messages('turn:1', 'report.md')
    session = _bclass_session(
        'bclass01', workspace=workspace, profile='ops', messages=messages,
    )

    result = store.backfill_session_artifacts(session, db_path=db_path)

    assert result['written'] == 1
    assert result['skipped'] == 0
    loaded = store.load_manifest_records(session, db_path=db_path)
    assert len(loaded) == 1
    row = loaded[0]
    assert row['path'] == 'report.md'
    assert row['profile'] == 'ops'
    assert row['turn_key'] == 'turn:1'
    assert row['source_tool'] == 'write_file'
    assert row['preview'] == 'file'


def test_backfill_session_artifacts_runs_even_with_turn_artifacts(tmp_path):
    """Sessions with turn_artifacts are not skipped — they may still have
    files the streaming pipeline missed. Re-extraction + upsert is idempotent."""
    db_path = tmp_path / 'manifest.db'
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    (workspace / 'report.md').write_text('hello', encoding='utf-8')

    messages = _write_file_turn_messages('turn:1', 'report.md')
    session = _bclass_session(
        'bclass02', workspace=workspace, profile='ops', messages=messages,
        turn_artifacts={'turn:1': [{'path': 'report.md', 'source_tool': 'write_file', 'preview': 'file'}]},
    )

    result = store.backfill_session_artifacts(session, db_path=db_path)

    # Re-extraction still runs; upsert is idempotent so the row lands once.
    assert result['written'] == 1
    loaded = store.load_manifest_records(session, db_path=db_path)
    assert len(loaded) == 1
    assert loaded[0]['path'] == 'report.md'


def test_backfill_session_artifacts_idempotent(tmp_path):
    db_path = tmp_path / 'manifest.db'
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    (workspace / 'report.md').write_text('hello', encoding='utf-8')

    messages = _write_file_turn_messages('turn:1', 'report.md')
    session = _bclass_session(
        'bclass03', workspace=workspace, profile='ops', messages=messages,
    )

    first = store.backfill_session_artifacts(session, db_path=db_path)
    second = store.backfill_session_artifacts(session, db_path=db_path)

    assert first['written'] == 1
    # Second run re-extracts the same rows; upsert ON CONFLICT keeps them
    # stable — no duplicate rows, count stays at 1.
    loaded_after_first = store.load_manifest_records(session, db_path=db_path)
    assert len(loaded_after_first) == 1
    store.backfill_session_artifacts(session, db_path=db_path)
    loaded_after_second = store.load_manifest_records(session, db_path=db_path)
    assert len(loaded_after_second) == 1


def test_backfill_session_artifacts_captures_prose_delivered_paths(tmp_path):
    """Files delivered only in assistant prose (no write_file tool call) are
    captured by the prose-path scan, mirroring _persist_turn_artifact_paths."""
    db_path = tmp_path / 'manifest.db'
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    (workspace / 'summary.md').write_text('hi', encoding='utf-8')

    # A turn with no write_file tool call; the assistant just mentions the
    # filename in its delivery text. The file exists on disk, so the prose
    # scan should pick it up.
    messages = [
        {'role': 'user', 'content': 'summarize', '_turn_key': 'turn:1'},
        {'role': 'assistant', 'content': 'done — see summary.md for the report.'},
    ]
    session = _bclass_session(
        'bclass04', workspace=workspace, profile='ops', messages=messages,
    )

    result = store.backfill_session_artifacts(session, db_path=db_path)

    assert result['written'] == 1
    loaded = store.load_manifest_records(session, db_path=db_path)
    assert len(loaded) == 1
    row = loaded[0]
    assert row['path'] == 'summary.md'
    assert row['profile'] == 'ops'
    assert row['source_tool'] == 'assistant_prose'
    assert row['preview'] == 'file'

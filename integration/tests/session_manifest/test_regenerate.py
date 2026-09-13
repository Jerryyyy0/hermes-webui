"""Destructive regenerate semantics without a revision/snapshot table."""
from pathlib import Path
import sqlite3
from types import SimpleNamespace


def _session(tmp_path):
    return SimpleNamespace(
        session_id='destructive-regen',
        profile='default',
        workspace=str(tmp_path),
        active_stream_id=None,
        messages=[
            {'role': 'user', 'content': 'first', '_turn_key': 'turn:0'},
            {'role': 'assistant', 'content': 'old.pdf'},
            {'role': 'user', 'content': 'second', '_turn_key': 'turn:2'},
            {'role': 'assistant', 'content': 'later.pdf'},
        ],
        context_messages=[
            {'role': 'user', 'content': 'first', '_turn_key': 'turn:0'},
            {'role': 'assistant', 'content': 'old.pdf'},
            {'role': 'user', 'content': 'second', '_turn_key': 'turn:2'},
            {'role': 'assistant', 'content': 'later.pdf'},
        ],
        tool_calls=[{'assistant_msg_idx': 1}, {'assistant_msg_idx': 3}],
        turn_artifacts={'turn:0': ['old.pdf'], 'turn:2': ['later.pdf']},
        async_delegation_origins={
            'old-child': {'turn_key': 'turn:0'},
            'later-child': {'turn_key': 'turn:2'},
        },
        async_delegation_cancellation={
            'state': 'cancelling',
            'delegation_ids': ['old-child', 'later-child'],
        },
        truncation_watermark=None,
        truncation_boundary=None,
        pending_regenerate=None,
        path=Path(tmp_path) / 'destructive-regen.json',
        save=lambda **kwargs: None,
    )


def test_prepare_regenerate_deletes_target_and_later_state_but_not_files(tmp_path, monkeypatch):
    from api.session_ops import prepare_destructive_regenerate
    from integration.session_manifest import store

    monkeypatch.setattr(store, 'STATE_DIR', tmp_path / 'state')
    for name in ('old.pdf', 'later.pdf'):
        (tmp_path / name).write_bytes(b'kept')
    session = _session(tmp_path)
    store.upsert_manifest_records(session, 'turn:0', [{'path': 'old.pdf'}])
    store.upsert_manifest_records(session, 'turn:2', [{'path': 'later.pdf'}])

    result = prepare_destructive_regenerate(session, 1)

    assert result['turn_key'] == 'turn:0'
    assert result['removed_turn_keys'] == ['turn:0', 'turn:2']
    assert session.messages == []
    assert session.context_messages == []
    assert session.tool_calls == []
    assert session.turn_artifacts == {}
    assert session.async_delegation_origins == {}
    assert session.async_delegation_cancellation is None
    assert store.load_manifest_records(session) == []
    assert (tmp_path / 'old.pdf').read_bytes() == b'kept'
    assert (tmp_path / 'later.pdf').read_bytes() == b'kept'


def test_regenerate_marker_reuses_original_turn_key_once(tmp_path, monkeypatch):
    from api.session_ops import (
        consume_pending_regenerate_turn_key,
        prepare_destructive_regenerate,
    )
    from integration.session_manifest import store

    monkeypatch.setattr(store, 'STATE_DIR', tmp_path / 'state')
    session = _session(tmp_path)
    prepare_destructive_regenerate(session, 3)

    assert consume_pending_regenerate_turn_key(session, 'second') == 'turn:2'
    assert consume_pending_regenerate_turn_key(session, 'second') == ''


def test_changed_prompt_discards_regenerate_marker(tmp_path, monkeypatch):
    from api.session_ops import (
        consume_pending_regenerate_turn_key,
        prepare_destructive_regenerate,
    )
    from integration.session_manifest import store

    monkeypatch.setattr(store, 'STATE_DIR', tmp_path / 'state')
    session = _session(tmp_path)
    prepare_destructive_regenerate(session, 3)

    assert consume_pending_regenerate_turn_key(session, 'changed') == ''
    assert session.pending_regenerate is None


def test_manifest_schema_does_not_create_revision_table(tmp_path, monkeypatch):
    from integration.session_manifest import store

    monkeypatch.setattr(store, 'STATE_DIR', tmp_path / 'state')
    session = _session(tmp_path)
    db_path = store._manifest_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute('CREATE TABLE session_manifest_revisions (session_id TEXT)')
    store.upsert_manifest_records(session, 'turn:0', [{'path': 'old.pdf'}])
    with store._connect() as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert 'session_manifest_records' in tables
    assert 'session_manifest_revisions' not in tables


def test_regenerate_shrink_survives_startup_recovery(tmp_path, monkeypatch):
    from api import config, models, session_recovery
    from api.session_ops import prepare_destructive_regenerate
    from integration.session_manifest import store

    session_dir = tmp_path / 'sessions'
    session_dir.mkdir()
    monkeypatch.setattr(models, 'SESSION_DIR', session_dir)
    monkeypatch.setattr(models, 'SESSION_INDEX_FILE', session_dir / '_index.json')
    monkeypatch.setattr(config, 'SESSION_DIR', session_dir)
    monkeypatch.setattr(store, 'SESSION_DIR', session_dir)
    monkeypatch.setattr(store, 'STATE_DIR', tmp_path / 'state')
    session = models.Session(
        session_id='regen-restart',
        workspace=str(tmp_path),
        messages=[
            {'role': 'user', 'content': 'first', '_turn_key': 'turn:0'},
            {'role': 'assistant', 'content': 'first reply'},
            {'role': 'user', 'content': 'second', '_turn_key': 'turn:2'},
            {'role': 'assistant', 'content': 'second reply'},
        ],
        context_messages=[
            {'role': 'user', 'content': 'first', '_turn_key': 'turn:0'},
            {'role': 'assistant', 'content': 'first reply'},
            {'role': 'user', 'content': 'second', '_turn_key': 'turn:2'},
            {'role': 'assistant', 'content': 'second reply'},
        ],
    )
    session.save()

    prepare_destructive_regenerate(session, 3)

    assert not session.path.with_suffix('.json.bak').exists()
    assert session_recovery.recover_all_sessions_on_startup(session_dir)['restored'] == 0
    restored = models.Session.load(session.session_id)
    assert [row['content'] for row in restored.messages] == ['first', 'first reply']
    assert restored.pending_regenerate['turn_key'] == 'turn:2'


def test_gateway_cancel_settles_current_turn_as_terminal_decision(monkeypatch):
    from api import gateway_chat, streaming

    calls = []
    session = SimpleNamespace(
        active_stream_id='stream-current',
        pending_turn_key='turn:2',
        save=lambda: calls.append('save'),
    )
    monkeypatch.setattr(
        streaming,
        '_materialize_pending_user_turn_before_error',
        lambda current: calls.append(('materialize', current)),
    )
    monkeypatch.setattr(
        streaming,
        '_snapshot_and_append_partial_on_error',
        lambda current, stream_id: calls.append(('snapshot', current, stream_id)),
    )
    monkeypatch.setattr(
        streaming,
        '_persist_turn_artifact_paths',
        lambda current, turn_key, **kwargs: calls.append(
            ('persist', current, turn_key, kwargs)
        ),
    )

    gateway_chat._settle_gateway_unfinished_artifacts(
        session,
        'stream-current',
        cancelled=True,
    )

    assert calls[-1] == (
        'persist',
        session,
        'turn:2',
        {'stream_id': 'stream-current', 'terminal_reason': 'cancelled'},
    )

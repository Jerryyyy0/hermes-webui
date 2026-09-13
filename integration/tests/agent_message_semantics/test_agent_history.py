"""Control provenance must survive WebUI history preparation and compaction."""
import copy

import pytest

from api.streaming import _sanitize_messages_for_api
from integration.agent_message_semantics.projection import drop_non_display_messages


@pytest.mark.parametrize('kind', ['async_delegation_completion', 'length_continuation', 'compaction_summary'])
def test_agent_history_preserves_control_provenance(kind):
    messages = [
        {'role': 'tool', 'tool_call_id': 'orphan', 'content': 'discard'},
        {'role': 'user', 'content': 'same text'},
        {'role': 'user', 'content': 'same text', '_hermes_message_class': 'context_anchor',
         '_hermes_scaffold_kind': kind, 'timestamp': 123, '_unrelated_private': True},
        {'role': 'assistant', 'content': 'answer'},
    ]
    before = copy.deepcopy(messages)
    prepared = _sanitize_messages_for_api(messages, preserve_agent_semantics=True)
    assert prepared[1]['_hermes_message_class'] == 'context_anchor'
    assert prepared[1]['_hermes_scaffold_kind'] == kind
    assert '_hermes_message_class' not in prepared[0]
    assert 'timestamp' not in prepared[1]
    assert '_unrelated_private' not in prepared[1]
    assert drop_non_display_messages(prepared) == [prepared[0], prepared[2]]
    assert _sanitize_messages_for_api(prepared) == _sanitize_messages_for_api(messages)
    assert messages == before


def test_legacy_semantics_are_normalized_without_forwarding_arbitrary_flags():
    prepared = _sanitize_messages_for_api([
        {'role': 'user', 'content': 'todo', '_todo_snapshot_synthetic': True},
    ], preserve_agent_semantics=True)
    assert prepared == [{'role': 'user', 'content': 'todo',
                         '_hermes_message_class': 'context_anchor',
                         '_hermes_scaffold_kind': 'todo_snapshot'}]


def test_real_agent_compaction_keeps_control_rows_hidden(tmp_path, monkeypatch):
    """Optional Agent contract test; uses its real SQLite writer and transport.

    Enable with HERMES_WEBUI_AGENT_DIR pointing at a compatible Agent checkout.
    No model request or real state is used.
    """
    state = pytest.importorskip('hermes_state')
    transport = pytest.importorskip('agent.transports.chat_completions')
    from api import models
    from api.routes import _message_window_for_display

    rows = [{'role': 'user', 'content': 'Research three topics'}]
    for index, kind in enumerate([
        'async_delegation_completion', 'async_delegation_completion',
        'async_delegation_completion', 'length_continuation',
    ]):
        rows.extend([
            {'role': 'user', 'content': f'Internal result {index}',
             '_hermes_message_class': 'context_anchor', '_hermes_scaffold_kind': kind},
            {'role': 'assistant', 'content': f'Answer {index}'},
        ])
    rows.append({'role': 'user', 'content': 'Internal result 0'})
    path = tmp_path / 'state.db'
    db = state.SessionDB(path)
    try:
        db.create_session('compact-test', source='webui')
        db.replace_messages('compact-test', rows)
        history = _sanitize_messages_for_api(rows, preserve_agent_semantics=True)
        # The retained tail is reinserted at a new timestamp by in-place compaction.
        summary = {'role': 'user', 'content': 'Summary',
                   '_hermes_message_class': 'context_anchor',
                   '_hermes_scaffold_kind': 'compaction_summary'}
        db.archive_and_compact('compact-test', [summary, *history])
        monkeypatch.setattr(models, '_active_state_db_path', lambda: path)
        restored = models.get_state_db_session_messages('compact-test')
        assert len(restored) == len(rows) + 1
        assert sum(m.get('_hermes_message_class') == 'context_anchor' for m in restored) == 5
        visible = drop_non_display_messages(restored)
        window, offset = _message_window_for_display(
            visible, msg_limit=50, msg_before=len(visible), turn_align=True,
        )
        assert offset == 0
        assert [m['content'] for m in window if m['role'] == 'user'] == [
            'Research three topics', 'Internal result 0',
        ]
        before = copy.deepcopy(history)
        wire = transport.ChatCompletionsTransport().convert_messages(history)
        assert all(not key.startswith('_') for m in wire for key in m)
        assert history == before
    finally:
        db.close()

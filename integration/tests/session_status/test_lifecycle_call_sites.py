from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_session_model_persists_error_timestamp_before_messages():
    src = (ROOT / "api/models.py").read_text(encoding="utf-8")
    assert "last_error_at=None" in src
    metadata = src[src.index("METADATA_FIELDS = ["):src.index("meta = {", src.index("METADATA_FIELDS = ["))]
    assert "'active_stream_id', 'last_error_at'" in metadata
    compact = src[src.index("def compact("):src.index("def _get_profile_home", src.index("def compact("))]
    assert "'last_error_at': self.last_error_at" in compact


def test_chat_start_clears_error_and_advances_own_message_cursor():
    src = (ROOT / "api/routes.py").read_text(encoding="utf-8")
    start = src.index("def _prepare_chat_start_session_for_stream")
    end = src.index("def _is_hidden_empty_session", start)
    block = src[start:end]
    assert block.index("s.last_error_at = None") < block.index("s.save()")
    assert "advance_read_until(s.profile, s.session_id, s.pending_started_at)" in block


def test_both_provider_error_paths_stamp_error_before_save():
    src = (ROOT / "api/streaming.py").read_text(encoding="utf-8")
    occurrences = []
    offset = 0
    while True:
        idx = src.find("_append_persisted_provider_error_message(", offset)
        if idx < 0:
            break
        save_idx = src.find("s.save()", idx)
        if save_idx > idx and "s.last_error_at = time.time()" in src[idx:save_idx]:
            occurrences.append(idx)
        offset = idx + 1
    assert len(occurrences) == 2


def test_cancel_path_does_not_set_error_timestamp():
    src = (ROOT / "api/streaming.py").read_text(encoding="utf-8")
    start = src.index("def _persist_cancelled_turn")
    end = src.index("def _cleanup_ephemeral_cancelled_turn", start)
    assert "last_error_at" not in src[start:end]

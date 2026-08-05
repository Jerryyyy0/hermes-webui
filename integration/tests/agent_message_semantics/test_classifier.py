"""Integration-level contract for Hermes Agent message semantics."""

from integration.agent_message_semantics.classifier import (
    is_context_anchor,
    is_internal_scaffold,
    is_non_anchor_control_message,
)
from integration.agent_message_semantics.audit import log_control_message


def test_new_internal_scaffold_is_hidden_from_visible_transcript():
    message = {
        "role": "user",
        "content": "[System: continue]",
        "_hermes_message_class": "internal_scaffold",
        "_hermes_scaffold_kind": "length_continuation",
    }

    assert is_internal_scaffold(message) is True
    assert is_non_anchor_control_message(message) is True


def test_context_anchor_is_hidden_but_recognized_after_state_db_replay():
    message = {
        "role": "user",
        "content": "[System: active model changed]",
        "_hermes_message_class": "context_anchor",
        "_hermes_scaffold_kind": "model_switch",
    }

    assert is_context_anchor(message) is True
    assert is_non_anchor_control_message(message) is True


def test_unknown_message_class_is_preserved():
    message = {
        "role": "user",
        "content": "Please handle this normally.",
        "_hermes_message_class": "future_extension",
        "_hermes_scaffold_kind": "future_kind",
    }

    assert is_non_anchor_control_message(message) is False


def test_control_audit_log_includes_message_content(caplog):
    control_message = {
        "role": "user",
        "content": "[System: run verification]",
        "_hermes_message_class": "internal_scaffold",
        "_hermes_scaffold_kind": "verification_stop",
    }

    with caplog.at_level("INFO"):
        log_control_message("display_drop", control_message)

    assert "verification_stop" in caplog.text
    assert "content='[System: run verification]'" in caplog.text


def test_context_anchor_audit_log_includes_original_model_content(caplog):
    context_anchor = {
        "role": "user",
        "content": "[Todo snapshot with model-only details]",
        "_hermes_message_class": "context_anchor",
        "_hermes_scaffold_kind": "todo_snapshot",
    }

    with caplog.at_level("INFO"):
        log_control_message("manifest_turn_skip", context_anchor)

    assert "content='[Todo snapshot with model-only details]'" in caplog.text


def test_legacy_control_audit_log_resolves_its_specific_kind(caplog):
    with caplog.at_level("INFO"):
        log_control_message("display_drop", {
            "role": "user",
            "content": "private legacy nudge",
            "_pre_verify_synthetic": True,
        })

    assert "class=internal_scaffold" in caplog.text
    assert "kind=pre_verify" in caplog.text
    assert "content='private legacy nudge'" in caplog.text

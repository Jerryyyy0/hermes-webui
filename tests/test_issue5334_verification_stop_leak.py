"""Regression tests for issue #5334.

The Hermes Agent runs an internal verify-before-finish loop. When it fires, it
preserves the attempted assistant answer as real interim content, then appends
a synthetic ``user`` nudge whose text looks like::

    [System: You edited code in this turn, but the workspace does not have fresh
     passing verification evidence yet...]

The nudge alone carries a structured marker
(``_verification_stop_synthetic`` for verify-on-stop,
``_pre_verify_synthetic`` for plugin ``pre_verify``) and must not reach the
visible transcript. The unmarked interim assistant answer must remain visible.

These tests pin the contract that a marker-flagged synthetic turn is dropped
from the merged display transcript while a normal user message is preserved.
"""

from api import streaming


VERIFICATION_STOP_NUDGE = (
    "[System: You edited code in this turn, but the workspace does not have "
    "fresh passing verification evidence yet.\n\n"
    "Verification status: unverified\n\n"
    "Changed paths:\n- `app/foo.py`\n\n"
    "Run the relevant verification command now.]"
)


# --------------------------------------------------------------------------- #
# Marker classifier + drop helper
# --------------------------------------------------------------------------- #
def test_verification_stop_synthetic_message_is_classified_as_control():
    msg = {
        "role": "user",
        "content": VERIFICATION_STOP_NUDGE,
        "_verification_stop_synthetic": True,
    }
    assert streaming._is_synthetic_control_message(msg) is True


def test_pre_verify_synthetic_message_is_classified_as_control():
    msg = {
        "role": "user",
        "content": "[System: run tests]",
        "_pre_verify_synthetic": True,
    }
    assert streaming._is_synthetic_control_message(msg) is True


def test_normal_user_message_is_not_classified_as_control():
    assert streaming._is_synthetic_control_message(
        {"role": "user", "content": "Please refactor the parser."}
    ) is False
    # A message that merely quotes the nudge text but lacks the marker is a real
    # message and must NOT be dropped (marker-based, not string-based, filter).
    assert streaming._is_synthetic_control_message(
        {"role": "user", "content": VERIFICATION_STOP_NUDGE}
    ) is False
    assert streaming._is_synthetic_control_message("not a dict") is False


def test_new_semantic_internal_scaffold_is_classified_as_control():
    assert streaming._is_synthetic_control_message({
        "role": "user",
        "content": "[System: continue]",
        "_hermes_message_class": "internal_scaffold",
        "_hermes_scaffold_kind": "length_continuation",
    }) is True


def test_new_semantic_scaffold_is_removed_but_real_assistants_are_retained():
    merged = streaming._merge_display_messages_after_agent_result(
        [{"role": "user", "content": "Fix it.", "_turn_key": "turn:8"}],
        [{"role": "user", "content": "Fix it.", "_turn_key": "turn:8"}],
        [
            {"role": "user", "content": "Fix it.", "_turn_key": "turn:8"},
            {"role": "assistant", "content": "I will verify this."},
            {
                "role": "user",
                "content": "[System: continue]",
                "_hermes_message_class": "internal_scaffold",
                "_hermes_scaffold_kind": "length_continuation",
            },
            {"role": "assistant", "content": "Verified. MEDIA: result.png"},
        ],
        "Fix it.",
        canonical_turn_key="turn:8",
    )

    assert [message["content"] for message in merged] == [
        "Fix it.",
        "I will verify this.",
        "Verified. MEDIA: result.png",
    ]


def test_drop_synthetic_control_messages_filters_only_flagged_rows():
    messages = [
        {"role": "user", "content": "Fix the bug."},
        {"role": "assistant", "content": "premature done"},
        {"role": "user", "content": VERIFICATION_STOP_NUDGE, "_verification_stop_synthetic": True},
        {"role": "assistant", "content": "Fixed and verified."},
    ]
    cleaned = streaming._drop_synthetic_control_messages(messages)
    contents = [m.get("content") for m in cleaned]
    assert contents == ["Fix the bug.", "premature done", "Fixed and verified."]
    assert all(
        not m.get("_verification_stop_synthetic") and not m.get("_pre_verify_synthetic")
        for m in cleaned
    )


# --------------------------------------------------------------------------- #
# End-to-end: the display-merge chokepoint that builds the visible transcript
# --------------------------------------------------------------------------- #
def test_verification_stop_nudge_never_appears_in_rendered_transcript():
    """The synthetic verify-stop turns must not survive display settlement."""
    previous_display = [{"role": "user", "content": "Fix the bug."}]
    previous_context = [{"role": "user", "content": "Fix the bug."}]
    result_messages = [
        {"role": "user", "content": "Fix the bug."},
        {"role": "assistant", "content": "premature done"},
        {"role": "user", "content": VERIFICATION_STOP_NUDGE, "_verification_stop_synthetic": True},
        {"role": "assistant", "content": "Ran tests; all green. Fixed."},
    ]

    merged = streaming._merge_display_messages_after_agent_result(
        previous_display,
        previous_context,
        result_messages,
        "Fix the bug.",
    )

    # The internal nudge text must never reach the rendered transcript.
    assert all(VERIFICATION_STOP_NUDGE not in str(m.get("content") or "") for m in merged)
    # No leftover synthetic scaffolding rows.
    assert all(not streaming._is_synthetic_control_message(m) for m in merged)
    # The real turn is preserved end to end.
    contents = [m.get("content") for m in merged]
    assert contents == [
        "Fix the bug.",
        "premature done",
        "Ran tests; all green. Fixed.",
    ]


def test_pre_verify_nudge_never_appears_in_rendered_transcript():
    previous_display = [{"role": "user", "content": "Ship the feature."}]
    previous_context = [{"role": "user", "content": "Ship the feature."}]
    result_messages = [
        {"role": "user", "content": "Ship the feature."},
        {"role": "assistant", "content": "premature done"},
        {"role": "user", "content": "[System: run tests]", "_pre_verify_synthetic": True},
        {"role": "assistant", "content": "Verified and clean."},
    ]

    merged = streaming._merge_display_messages_after_agent_result(
        previous_display,
        previous_context,
        result_messages,
        "Ship the feature.",
    )

    assert all(not streaming._is_synthetic_control_message(m) for m in merged)
    contents = [m.get("content") for m in merged]
    assert contents == ["Ship the feature.", "premature done", "Verified and clean."]


def test_synthetic_nudge_in_previous_context_is_not_backfilled():
    previous_display = [
        {"role": "user", "content": "Fix the bug.", "_turn_key": "turn:8"},
        {"role": "assistant", "content": "premature done"},
    ]
    previous_context = [
        {"role": "user", "content": "Fix the bug.", "_turn_key": "turn:8"},
        {"role": "assistant", "content": "premature done"},
        {
            "role": "user",
            "content": VERIFICATION_STOP_NUDGE,
            "_verification_stop_synthetic": True,
        },
    ]
    result_messages = [
        {"role": "user", "content": "Fix the bug.", "_turn_key": "turn:8"},
        {"role": "assistant", "content": "premature done"},
        {"role": "assistant", "content": "Ran tests; all green. Fixed."},
    ]

    merged = streaming._merge_display_messages_after_agent_result(
        previous_display,
        previous_context,
        result_messages,
        "Fix the bug.",
        canonical_turn_key="turn:8",
    )

    assert [message.get("content") for message in merged] == [
        "Fix the bug.",
        "premature done",
        "Ran tests; all green. Fixed.",
    ]


def test_normal_user_message_is_not_excluded_from_transcript():
    """A regular user turn (no marker) must still render, even mid-transcript."""
    previous_display = [
        {"role": "user", "content": "First question."},
        {"role": "assistant", "content": "First answer."},
    ]
    previous_context = [
        {"role": "user", "content": "First question."},
        {"role": "assistant", "content": "First answer."},
    ]
    result_messages = [
        {"role": "user", "content": "First question."},
        {"role": "assistant", "content": "First answer."},
        {"role": "user", "content": "Second question."},
        {"role": "assistant", "content": "Second answer."},
    ]

    merged = streaming._merge_display_messages_after_agent_result(
        previous_display,
        previous_context,
        result_messages,
        "Second question.",
    )

    contents = [m.get("content") for m in merged]
    assert contents == [
        "First question.",
        "First answer.",
        "Second question.",
        "Second answer.",
    ]


def test_leaked_nudge_in_prior_display_is_scrubbed_on_next_settlement():
    """A previously-leaked synthetic row in prior display is dropped on re-merge."""
    previous_display = [
        {"role": "user", "content": "Fix the bug."},
        # Simulates a nudge that leaked in a prior build before the fix landed.
        {"role": "user", "content": VERIFICATION_STOP_NUDGE, "_verification_stop_synthetic": True},
        {"role": "assistant", "content": "Fixed and verified."},
    ]
    previous_context = [
        {"role": "user", "content": "Fix the bug."},
        {"role": "assistant", "content": "Fixed and verified."},
    ]
    result_messages = [
        {"role": "user", "content": "Fix the bug."},
        {"role": "assistant", "content": "Fixed and verified."},
        {"role": "user", "content": "Now add a test."},
        {"role": "assistant", "content": "Test added."},
    ]

    merged = streaming._merge_display_messages_after_agent_result(
        previous_display,
        previous_context,
        result_messages,
        "Now add a test.",
    )

    assert all(VERIFICATION_STOP_NUDGE not in str(m.get("content") or "") for m in merged)
    assert all(not streaming._is_synthetic_control_message(m) for m in merged)

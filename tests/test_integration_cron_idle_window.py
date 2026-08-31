"""Contract tests for Cron Hub idle-window metadata."""

import pytest

from integration.crons.handlers import _normalize_idle_window


def test_idle_window_accepts_complete_one_shot_schedule_metadata():
    assert _normalize_idle_window(
        {
            "idle_window": {
                "start_schedule": {
                    "kind": "once",
                    "run_at": "2026-09-01T22:00:00+08:00",
                },
                "end_schedule": {
                    "kind": "once",
                    "run_at": "2026-09-02T06:00:00+08:00",
                },
            }
        }
    ) == {
        "start_schedule": {
            "kind": "once",
            "run_at": "2026-09-01T22:00:00+08:00",
            "display": "2026-09-01T22:00:00+08:00",
        },
        "end_schedule": {
            "kind": "once",
            "run_at": "2026-09-02T06:00:00+08:00",
            "display": "2026-09-02T06:00:00+08:00",
        },
    }


def test_idle_window_accepts_complete_recurring_cron_schedule_metadata():
    assert _normalize_idle_window(
        {
            "idle_window": {
                "start_schedule": {"kind": "cron", "expr": "0 22 * * *"},
                "end_schedule": {"kind": "cron", "expr": "0 6 * * *"},
            }
        }
    ) == {
        "start_schedule": {
            "kind": "cron",
            "expr": "0 22 * * *",
            "display": "0 22 * * *",
        },
        "end_schedule": {
            "kind": "cron",
            "expr": "0 6 * * *",
            "display": "0 6 * * *",
        },
    }


def test_idle_window_is_absent_without_changing_an_existing_value():
    existing = {"start_schedule": {"kind": "cron", "expr": "0 9 * * *"}}

    assert _normalize_idle_window({}, default=existing) == existing


def test_idle_window_accepts_null_for_explicit_clear():
    assert _normalize_idle_window({"idle_window": None}) is None


@pytest.mark.parametrize(
    "body",
    [
        {"idle_window": {"start_schedule": {"kind": "cron", "expr": "0 22 * * *"}}},
        {
            "idle_window": {
                "start_schedule": {"kind": "once", "run_at": "2026-09-01T22:00:00+08:00"},
                "end_schedule": {"kind": "cron", "expr": "0 6 * * *"},
            }
        },
        {
            "idle_window": {
                "start_schedule": {"kind": "once", "run_at": "2026-09-02T06:00:00+08:00"},
                "end_schedule": {"kind": "once", "run_at": "2026-09-01T22:00:00+08:00"},
            }
        },
        {"idle_start_time": "22:00", "idle_end_time": "06:00"},
    ],
)
def test_idle_window_rejects_partial_invalid_or_legacy_flat_values(body):
    with pytest.raises(ValueError):
        _normalize_idle_window(body)

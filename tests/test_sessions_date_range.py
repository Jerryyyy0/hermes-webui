"""Tests for GET /api/sessions optional start_at / end_at date-range filtering."""

from __future__ import annotations

from urllib.parse import urlparse

import pytest


def _row(session_id: str, *, last_message_at: float | None = 100.0, **extra):
    row = {
        "session_id": session_id,
        "profile": "default",
        "last_message_at": last_message_at,
        "updated_at": last_message_at,
        "title": session_id,
        "message_count": 1,
    }
    row.update(extra)
    return row


def test_parse_date_range_query_absent():
    from api.session_listing import parse_date_range_query

    parsed = urlparse("/api/sessions?profile=default")
    assert parse_date_range_query(parsed) is None


def test_parse_date_range_query_start_only():
    from api.session_listing import parse_date_range_query

    parsed = urlparse("/api/sessions?start_at=100")
    assert parse_date_range_query(parsed) == (100.0, None)


def test_parse_date_range_query_end_only():
    from api.session_listing import parse_date_range_query

    parsed = urlparse("/api/sessions?end_at=200")
    assert parse_date_range_query(parsed) == (None, 200.0)


def test_parse_date_range_query_both_bounds():
    from api.session_listing import parse_date_range_query

    parsed = urlparse("/api/sessions?start_at=100&end_at=200")
    assert parse_date_range_query(parsed) == (100.0, 200.0)


def test_parse_date_range_query_invalid_start():
    from api.session_listing import parse_date_range_query

    parsed = urlparse("/api/sessions?start_at=bad")
    assert parse_date_range_query(parsed) == "invalid_start_at"


def test_parse_date_range_query_invalid_end():
    from api.session_listing import parse_date_range_query

    parsed = urlparse("/api/sessions?end_at=bad")
    assert parse_date_range_query(parsed) == "invalid_end_at"


def test_parse_date_range_query_start_after_end():
    from api.session_listing import parse_date_range_query

    parsed = urlparse("/api/sessions?start_at=300&end_at=100")
    assert parse_date_range_query(parsed) == "start_after_end"


def test_filter_sessions_by_date_range_inclusive_bounds():
    from api.session_listing import filter_sessions_by_date_range

    rows = [
        _row("before", last_message_at=50.0),
        _row("start", last_message_at=100.0),
        _row("middle", last_message_at=150.0),
        _row("end", last_message_at=200.0),
        _row("after", last_message_at=250.0),
    ]
    filtered = filter_sessions_by_date_range(rows, start_at=100.0, end_at=200.0)
    assert [s["session_id"] for s in filtered] == ["start", "middle", "end"]


def test_filter_sessions_by_date_range_start_only():
    from api.session_listing import filter_sessions_by_date_range

    rows = [_row("old", last_message_at=10.0), _row("new", last_message_at=90.0)]
    filtered = filter_sessions_by_date_range(rows, start_at=50.0, end_at=None)
    assert [s["session_id"] for s in filtered] == ["new"]


def test_filter_sessions_by_date_range_end_only():
    from api.session_listing import filter_sessions_by_date_range

    rows = [_row("old", last_message_at=10.0), _row("new", last_message_at=90.0)]
    filtered = filter_sessions_by_date_range(rows, start_at=None, end_at=50.0)
    assert [s["session_id"] for s in filtered] == ["old"]


def test_filter_sessions_by_date_range_excludes_missing_last_message_at():
    from api.session_listing import filter_sessions_by_date_range

    rows = [
        _row("missing", last_message_at=None),
        _row("empty", last_message_at=""),
        _row("bad", last_message_at="nope"),
        _row("ok", last_message_at=100.0),
    ]
    filtered = filter_sessions_by_date_range(rows, start_at=0.0, end_at=200.0)
    assert [s["session_id"] for s in filtered] == ["ok"]


def test_filter_sessions_by_date_range_does_not_fallback_to_updated_at():
    from api.session_listing import filter_sessions_by_date_range

    rows = [_row("only-updated", last_message_at=None, updated_at=100.0)]
    filtered = filter_sessions_by_date_range(rows, start_at=0.0, end_at=200.0)
    assert filtered == []


def test_sessions_date_range_payload():
    from api.routes import _sessions_date_range_payload

    assert _sessions_date_range_payload(None) == {}
    assert _sessions_date_range_payload((100.0, None)) == {"start_at": 100.0}
    assert _sessions_date_range_payload((None, 200.0)) == {"end_at": 200.0}
    assert _sessions_date_range_payload((100.0, 200.0)) == {
        "start_at": 100.0,
        "end_at": 200.0,
    }


def test_routes_sessions_handler_applies_date_filter_before_pagination():
    from pathlib import Path

    repo_root = Path(__file__).parent.parent
    src = (repo_root / "api" / "routes.py").read_text(encoding="utf-8")
    handler_idx = src.find('parsed.path == "/api/sessions":')
    assert handler_idx > 0
    next_handler = src.find('parsed.path == "/api/projects":', handler_idx)
    block = src[handler_idx:next_handler]
    assert "parse_date_range_query(parsed)" in block
    assert "filter_sessions_by_date_range" in block
    assert "profile_page_date_filter" in block
    assert 'diag.stage("date_filter")' in block

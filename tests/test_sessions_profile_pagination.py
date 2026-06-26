"""Tests for per-profile /api/sessions pagination (?profile=&offset=&limit=)."""

from __future__ import annotations

from urllib.parse import urlparse

import pytest


def _row(session_id: str, *, profile: str = "haku", ts: float = 1.0, **extra):
    row = {
        "session_id": session_id,
        "profile": profile,
        "last_message_at": ts,
        "updated_at": ts,
        "title": session_id,
        "message_count": 1,
    }
    row.update(extra)
    return row


def test_parse_sessions_profile_pagination_query():
    from api.session_listing import parse_profile_pagination_query

    parsed = urlparse("/api/sessions?profile=kinni&offset=5&limit=20")
    assert parse_profile_pagination_query(parsed) == ("kinni", 5, 20)

    parsed = urlparse("/api/sessions?profile=kinni")
    assert parse_profile_pagination_query(parsed) == ("kinni", 0, 15)

    parsed = urlparse("/api/sessions?all_profiles=1")
    assert parse_profile_pagination_query(parsed) is None


def test_parse_sessions_profile_pagination_query_clamps_limit():
    from api.session_listing import parse_profile_pagination_query

    parsed = urlparse("/api/sessions?profile=kinni&limit=999")
    assert parse_profile_pagination_query(parsed) == ("kinni", 0, 50)


def test_paginate_session_rows_first_page_and_load_more():
    from api.session_listing import paginate_session_rows

    rows = [_row(f"s{i}", ts=float(100 - i)) for i in range(10)]
    first = paginate_session_rows(rows, offset=0, limit=3)
    assert first["total_count"] == 10
    assert len(first["sessions"]) == 3
    assert first["has_more"] is True
    assert first["sessions"][0]["session_id"] == "s0"

    second = paginate_session_rows(rows, offset=3, limit=3)
    assert len(second["sessions"]) == 3
    assert second["sessions"][0]["session_id"] == "s3"
    assert second["has_more"] is True

    last = paginate_session_rows(rows, offset=9, limit=3)
    assert len(last["sessions"]) == 1
    assert last["has_more"] is False


def test_paginate_session_rows_must_show_prefix_without_duplicates():
    from api.session_listing import paginate_session_rows

    rows = [
        _row("pinned", ts=1.0, pinned=True),
        _row("s0", ts=100.0),
        _row("s1", ts=99.0),
        _row("s2", ts=98.0),
    ]
    first = paginate_session_rows(rows, offset=0, limit=1)
    ids = [s["session_id"] for s in first["sessions"]]
    assert ids == ["pinned"]
    assert len(first["sessions"]) == 1
    assert first["has_more"] is True

    second = paginate_session_rows(rows, offset=1, limit=2)
    ids = [s["session_id"] for s in second["sessions"]]
    assert ids == ["s0", "s1"]
    assert "pinned" not in ids

    third = paginate_session_rows(rows, offset=3, limit=2)
    ids = [s["session_id"] for s in third["sessions"]]
    assert ids == ["s2"]
    assert "pinned" not in ids


def test_paginate_strict_limit_with_must_show():
    from api.session_listing import paginate_session_rows

    rows = [
        _row("pin-a", ts=90.0, pinned=True),
        _row("pin-b", ts=80.0, pinned=True),
        _row("stream", ts=70.0, is_streaming=True),
        *[_row(f"s{i}", ts=float(60 - i)) for i in range(10)],
    ]
    first = paginate_session_rows(rows, offset=0, limit=5)
    assert len(first["sessions"]) == 5
    assert [s["session_id"] for s in first["sessions"]] == [
        "pin-a", "pin-b", "stream", "s0", "s1",
    ]
    assert first["has_more"] is True

    second = paginate_session_rows(rows, offset=5, limit=5)
    assert len(second["sessions"]) == 5
    assert [s["session_id"] for s in second["sessions"]] == ["s2", "s3", "s4", "s5", "s6"]


def test_paginate_many_pinned_span_pages():
    from api.session_listing import paginate_session_rows

    rows = [
        *[_row(f"pin{i}", ts=float(200 - i), pinned=True) for i in range(8)],
        *[_row(f"s{i}", ts=float(100 - i)) for i in range(20)],
    ]
    first = paginate_session_rows(rows, offset=0, limit=5)
    assert [s["session_id"] for s in first["sessions"]] == [
        "pin0", "pin1", "pin2", "pin3", "pin4",
    ]

    second = paginate_session_rows(rows, offset=5, limit=5)
    assert [s["session_id"] for s in second["sessions"]] == [
        "pin5", "pin6", "pin7", "s0", "s1",
    ]

    third = paginate_session_rows(rows, offset=8, limit=5)
    ids = [s["session_id"] for s in third["sessions"]]
    assert ids == ["s0", "s1", "s2", "s3", "s4"]
    assert not any(s.get("pinned") for s in third["sessions"])


def test_paginate_no_pinned_duplicate_across_pages():
    from api.session_listing import paginate_session_rows

    rows = [_row("pinned", ts=1.0, pinned=True)]
    rows.extend(_row(f"s{i}", ts=float(200 - i)) for i in range(30))
    seen_pinned = 0
    offset = 0
    limit = 10
    while True:
        page = paginate_session_rows(rows, offset=offset, limit=limit)
        for session in page["sessions"]:
            if session.get("pinned"):
                seen_pinned += 1
        if not page["has_more"]:
            break
        offset += len(page["sessions"])
    assert seen_pinned == 1


def test_paginate_has_more_boundary():
    from api.session_listing import paginate_session_rows

    rows = [_row(f"s{i}", ts=float(10 - i)) for i in range(7)]
    page = paginate_session_rows(rows, offset=6, limit=3)
    assert len(page["sessions"]) == 1
    assert page["has_more"] is False


def test_paginate_pinned_rows_sort_by_pinned_at():
    from api.session_listing import paginate_session_rows

    rows = [
        _row("old-pin", ts=100.0, pinned=True, pinned_at=10.0),
        _row("new-pin", ts=1.0, pinned=True, pinned_at=90.0),
        _row("s0", ts=50.0),
    ]
    first = paginate_session_rows(rows, offset=0, limit=3)
    assert [s["session_id"] for s in first["sessions"]] == ["new-pin", "old-pin", "s0"]


def test_finalize_sessions_for_profile_filters_rows():
    from api.routes import finalize_sessions_for_profile

    merged = [
        _row("a", profile="haku"),
        _row("b", profile="kinni"),
    ]
    scoped = finalize_sessions_for_profile(
        merged,
        settings={"show_previous_messaging_sessions": True, "show_cli_sessions": False},
        profile_name="kinni",
    )
    assert [s["session_id"] for s in scoped] == ["b"]


def test_finalize_sessions_for_profile_root_alias(monkeypatch):
    import api.profiles as p
    from api.routes import finalize_sessions_for_profile, _profiles_match

    monkeypatch.setattr(
        p,
        "list_profiles_api",
        lambda: [
            {"name": "kinni", "is_default": True, "path": str(p._DEFAULT_HERMES_HOME)},
        ],
    )
    p._invalidate_root_profile_cache()

    merged = [
        _row("root-default", profile="default"),
        _row("other", profile="haku"),
    ]
    assert _profiles_match("default", "kinni") is True
    scoped = finalize_sessions_for_profile(
        merged,
        settings={"show_previous_messaging_sessions": True, "show_cli_sessions": False},
        profile_name="kinni",
    )
    assert [s["session_id"] for s in scoped] == ["root-default"]


def test_all_profiles_handler_still_returns_flat_sessions_shape():
    from pathlib import Path

    repo_root = Path(__file__).parent.parent
    src = (repo_root / "api" / "routes.py").read_text(encoding="utf-8")
    handler_idx = src.find('parsed.path == "/api/sessions":')
    assert handler_idx > 0
    next_handler = src.find('parsed.path == "/api/projects":', handler_idx)
    block = src[handler_idx:next_handler]
    assert 'profile_pagination = parse_profile_pagination_query(parsed)' in block
    assert '"sessions": safe_merged' in block
    assert '"profiles":' not in block


def test_static_sessions_js_uses_profile_pagination_for_cross_profile_view():
    from pathlib import Path

    repo_root = Path(__file__).parent.parent
    src = (repo_root / "static" / "sessions.js").read_text(encoding="utf-8")

    assert "_showAllProfiles ? '?all_profiles=1' : ''" not in src
    assert "api('/api/sessions',{timeoutToast:false})" in src
    assert "/api/sessions?profile=" in src
    assert "_loadMoreProfileSessions" in src
    assert "_profileSessionState" in src
    assert "regularOffset:sessions.length" in src

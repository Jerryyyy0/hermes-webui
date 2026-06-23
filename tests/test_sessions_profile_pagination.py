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


def test_paginate_session_rows_must_show_on_first_page_only():
    from api.session_listing import paginate_session_rows

    rows = [
        _row("pinned", ts=1.0, pinned=True),
        _row("s0", ts=100.0),
        _row("s1", ts=99.0),
        _row("s2", ts=98.0),
    ]
    first = paginate_session_rows(rows, offset=0, limit=1)
    ids = [s["session_id"] for s in first["sessions"]]
    assert "pinned" in ids
    assert len(first["sessions"]) == 2  # must_show + 1 regular
    assert first["has_more"] is True

    second = paginate_session_rows(rows, offset=1, limit=2)
    ids = [s["session_id"] for s in second["sessions"]]
    assert "pinned" not in ids


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

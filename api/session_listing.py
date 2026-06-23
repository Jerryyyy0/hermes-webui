"""Pure sidebar session-list helpers for GET /api/sessions profile pagination.

Pipeline builders that depend on api.routes stay in routes.py (strategy A).
"""

from __future__ import annotations

from urllib.parse import parse_qs

SESSION_PROFILE_PAGE_DEFAULT_LIMIT = 15
SESSION_PROFILE_PAGE_MAX_LIMIT = 50


def session_sidebar_timestamp(session: dict) -> float:
    value = session.get("last_message_at") or session.get("updated_at", 0) or 0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def parse_profile_pagination_query(parsed) -> tuple[str, int, int] | None:
    """Return (profile, offset, limit) when ``profile`` is set, else None."""
    qs = parse_qs(parsed.query)
    profile = qs.get("profile", [""])[0].strip()
    if not profile:
        return None
    try:
        offset = max(0, int(qs.get("offset", ["0"])[0]))
    except (ValueError, TypeError):
        offset = 0
    try:
        limit = int(qs.get("limit", [str(SESSION_PROFILE_PAGE_DEFAULT_LIMIT)])[0])
    except (ValueError, TypeError):
        limit = SESSION_PROFILE_PAGE_DEFAULT_LIMIT
    limit = max(1, min(limit, SESSION_PROFILE_PAGE_MAX_LIMIT))
    return profile, offset, limit


# Back-compat alias for callers that used the routes-local name.
_parse_sessions_profile_pagination_query = parse_profile_pagination_query


def session_row_must_show(session: dict, *, active_session_id: str | None = None) -> bool:
    sid = str(session.get("session_id") or "").strip()
    if active_session_id and sid and sid == active_session_id:
        return True
    if session.get("is_streaming") or session.get("active_stream_id") or session.get("pending_user_message"):
        return True
    if session.get("pinned"):
        return True
    attention = session.get("attention")
    if isinstance(attention, dict):
        count = attention.get("count")
        if isinstance(count, (int, float)) and float(count) > 0:
            return True
    return False


def paginate_session_rows(
    rows: list[dict],
    *,
    offset: int,
    limit: int,
    active_session_id: str | None = None,
) -> dict:
    """Paginate sidebar rows with must-show extras on the first page only."""
    sorted_rows = sorted(rows, key=session_sidebar_timestamp, reverse=True)
    must_show: list[dict] = []
    must_show_ids: set[str] = set()
    if offset == 0:
        for session in sorted_rows:
            if session_row_must_show(session, active_session_id=active_session_id):
                must_show.append(session)
                sid = str(session.get("session_id") or "").strip()
                if sid:
                    must_show_ids.add(sid)
    regular_rows = [
        session
        for session in sorted_rows
        if str(session.get("session_id") or "").strip() not in must_show_ids
    ]
    if offset == 0:
        page_regular = regular_rows[:limit]
        page = must_show + page_regular
        has_more = len(page) < len(sorted_rows)
    else:
        page = regular_rows[offset:offset + limit]
        has_more = (offset + len(page)) < len(regular_rows)
    return {
        "sessions": page,
        "total_count": len(sorted_rows),
        "offset": offset,
        "limit": limit,
        "has_more": has_more,
    }

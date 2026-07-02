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


def session_pinned_sort_timestamp(session: dict) -> float:
    raw = session.get("pinned_at")
    if raw is not None:
        try:
            return float(raw)
        except (TypeError, ValueError):
            pass
    return 0.0


def _sort_pinned_rows(rows: list[dict]) -> list[dict]:
    return sorted(rows, key=session_pinned_sort_timestamp, reverse=True)


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


def parse_date_range_query(parsed) -> tuple[float | None, float | None] | str | None:
    """Return (start_at, end_at) when either bound is set, else None.

    On validation failure returns an error code string for the handler to map
    to HTTP 400.
    """
    qs = parse_qs(parsed.query)
    raw_start = qs.get("start_at", [""])[0].strip()
    raw_end = qs.get("end_at", [""])[0].strip()
    if not raw_start and not raw_end:
        return None

    start_at = end_at = None
    if raw_start:
        try:
            start_at = float(raw_start)
        except (TypeError, ValueError):
            return "invalid_start_at"
    if raw_end:
        try:
            end_at = float(raw_end)
        except (TypeError, ValueError):
            return "invalid_end_at"
    if start_at is not None and end_at is not None and start_at > end_at:
        return "start_after_end"
    return start_at, end_at


def filter_sessions_by_date_range(
    rows: list[dict],
    *,
    start_at: float | None,
    end_at: float | None,
) -> list[dict]:
    """Keep rows whose ``last_message_at`` falls within the inclusive bounds."""
    out = []
    for row in rows:
        raw = row.get("last_message_at")
        if raw is None or raw == "":
            continue
        try:
            ts = float(raw)
        except (TypeError, ValueError):
            continue
        if start_at is not None and ts < start_at:
            continue
        if end_at is not None and ts > end_at:
            continue
        out.append(row)
    return out


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


def _sort_by_sidebar_timestamp(rows: list[dict]) -> list[dict]:
    return sorted(rows, key=session_sidebar_timestamp, reverse=True)


def _partition_virtual_session_rows(
    rows: list[dict],
    *,
    active_session_id: str | None = None,
) -> list[dict]:
    """Build virtual sidebar list: priority prefix (must-show) then regular rows."""
    sorted_rows = _sort_by_sidebar_timestamp(rows)
    priority_pinned: list[dict] = []
    priority_other: list[dict] = []
    priority_ids: set[str] = set()
    for session in sorted_rows:
        if not session_row_must_show(session, active_session_id=active_session_id):
            continue
        sid = str(session.get("session_id") or "").strip()
        if sid and sid in priority_ids:
            continue
        if sid:
            priority_ids.add(sid)
        if session.get("pinned"):
            priority_pinned.append(session)
        else:
            priority_other.append(session)
    priority_pinned = _sort_pinned_rows(priority_pinned)
    regular_rows = [
        session
        for session in sorted_rows
        if str(session.get("session_id") or "").strip() not in priority_ids
    ]
    return priority_pinned + priority_other + regular_rows


def paginate_session_rows(
    rows: list[dict],
    *,
    offset: int,
    limit: int,
    active_session_id: str | None = None,
) -> dict:
    """Paginate sidebar rows on a virtual list: must-show prefix, then regular rows."""
    virtual = _partition_virtual_session_rows(rows, active_session_id=active_session_id)
    page = virtual[offset:offset + limit]
    return {
        "sessions": page,
        "total_count": len(virtual),
        "offset": offset,
        "limit": limit,
        "has_more": (offset + len(page)) < len(virtual),
    }

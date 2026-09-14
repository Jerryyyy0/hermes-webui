"""Build the assistant profile overview without changing core WebUI models."""

from __future__ import annotations

import logging
import math
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from api.profiles import (
    _profiles_match,
    cron_profile_context_for_home,
    get_hermes_home_for_profile,
    list_profiles_api,
)
from integration.agent_message_semantics.projection import drop_non_display_messages
from integration.profiles.enrich import enrich_profiles_response
from integration.skills.local_skills import list_installed
from integration.skills.mtime_utils import read_local_skill_mtime
from integration.skills.paths import skills_dir_for_profile

logger = logging.getLogger(__name__)

SHANGHAI = ZoneInfo("Asia/Shanghai")
SAFE_BUFFER = 1.6
RAW_FILE_SPECS = {
    "soul": ("SOUL.md", "SOUL.md"),
    "memory": ("MEMORY.md", "memories/MEMORY.md"),
    "user": ("USER.md", "memories/USER.md"),
}


def resolve_profile(profile_name: str) -> tuple[dict[str, Any], Path]:
    """Return an enriched profile and its trusted Hermes home."""
    requested = str(profile_name or "").strip()
    if not requested:
        raise ValueError("缺少 profile")

    payload = enrich_profiles_response({"profiles": list_profiles_api()})
    profiles = [item for item in payload.get("profiles", []) if isinstance(item, dict)]
    profile = next(
        (item for item in profiles if _profiles_match(item.get("name"), requested)),
        None,
    )
    if profile is None:
        raise LookupError(f"未知的 profile: {requested}")
    resolved_name = str(profile.get("name") or requested)
    return profile, Path(get_hermes_home_for_profile(resolved_name))


def _parse_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        try:
            parsed = datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            return None
    else:
        raw = str(value).strip()
        if not raw:
            return None
        if raw.endswith("Z"):
            raw = f"{raw[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            try:
                parsed = datetime.fromtimestamp(float(raw), tz=timezone.utc)
            except (ValueError, OSError, OverflowError):
                return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI)
    return parsed.astimezone(SHANGHAI)


def _iso_seconds(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value else None


def _message_has_content(message: dict[str, Any]) -> bool:
    content = message.get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, (list, dict)):
        return bool(content)
    return content is not None


def _message_datetime(message: dict[str, Any]) -> datetime | None:
    return _parse_datetime(message.get("_ts") or message.get("timestamp"))


def _is_final_assistant_reply(message: Any) -> bool:
    if not isinstance(message, dict):
        return False
    if str(message.get("role") or "").strip().lower() != "assistant":
        return False
    if message.get("tool_calls") or message.get("_error") or message.get("_partial"):
        return False
    if str(message.get("finish_reason") or "").strip() in {
        "incomplete",
        "verification_required",
        "verify_hook_continue",
    }:
        return False
    return _message_has_content(message)


def _session_source(row: dict[str, Any], session: Any = None) -> str:
    for key in ("source_tag", "raw_source", "session_source", "source"):
        value = row.get(key)
        if value:
            return str(value).strip().lower()
    if session is not None:
        for key in ("source_tag", "raw_source", "session_source", "source"):
            value = getattr(session, key, None)
            if value:
                return str(value).strip().lower()
    return ""


def _is_cron_session(session_id: str, source: str) -> bool:
    return source == "cron" or session_id.startswith("cron_")


def _is_conversation_session(session_id: str, source: str) -> bool:
    return not _is_cron_session(session_id, source) and source in {"", "webui"}


def _collect_sessions(profile_name: str, start: date, end: date) -> tuple[int, Counter]:
    """Return ordinary conversation count and final assistant replies by day."""
    from api.models import all_sessions, get_session_for_scan

    conversation_count = 0
    daily_counts: Counter = Counter()
    for row in all_sessions(include_lineage_metadata=False):
        if not isinstance(row, dict) or not _profiles_match(row.get("profile"), profile_name):
            continue
        session_id = str(row.get("session_id") or "").strip()
        if not session_id:
            continue
        row_source = _session_source(row)
        if row_source and not (
            _is_conversation_session(session_id, row_source)
            or _is_cron_session(session_id, row_source)
        ):
            continue

        try:
            session = get_session_for_scan(session_id)
        except (KeyError, OSError, ValueError):
            session = None
        source = _session_source(row, session) if session is not None else row_source
        if not (_is_conversation_session(session_id, source) or _is_cron_session(session_id, source)):
            continue
        if _is_conversation_session(session_id, source):
            conversation_count += 1
        if session is None:
            continue
        messages = drop_non_display_messages(
            getattr(session, "messages", []),
            action="profile_overview_drop",
            session_id=session_id,
            background_task_origins=getattr(session, "async_delegation_origins", None),
        )
        for message in messages:
            if not _is_final_assistant_reply(message):
                continue
            message_at = _message_datetime(message)
            if message_at is None or not start <= message_at.date() <= end:
                continue
            daily_counts[message_at.date()] += 1
    return conversation_count, daily_counts


def _collect_cron_count(home: Path) -> int:
    try:
        from cron.jobs import list_jobs

        with cron_profile_context_for_home(home):
            jobs = list_jobs(include_disabled=True) or []
        active_states = {"scheduled", "running"}
        count = 0
        for job in jobs:
            if not isinstance(job, dict) or not job.get("enabled", True):
                continue
            # list_jobs() normalizes legacy enabled jobs without a state to
            # "scheduled". Keep the fallback here for compatibility with older
            # Agent versions that may return the unnormalized shape.
            state = str(job.get("state") or "scheduled").strip().lower()
            if state in active_states:
                count += 1
        return count
    except Exception:
        logger.debug("profile overview cron listing failed for %s", home, exc_info=True)
        return 0


def _percentile_80(values: list[int]) -> float:
    """Linear interpolation, matching the common percentile definition."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    position = (len(ordered) - 1) * 0.8
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[lower])
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _activity_level(ratio: float) -> int:
    if ratio <= 0:
        return 0
    if ratio <= 0.20:
        return 1
    if ratio <= 0.45:
        return 2
    if ratio <= 0.70:
        return 3
    return 4


def _month_key_for_week(week_start: date, start: date, end: date) -> str:
    in_range = [week_start + timedelta(days=i) for i in range(7)]
    eligible = [day for day in in_range if start <= day <= end]
    counts = Counter((day.year, day.month) for day in eligible)
    if not counts:
        return ""
    (year, month), _ = max(counts.items(), key=lambda item: (item[1], -item[0][0], -item[0][1]))
    return f"{year:04d}-{month:02d}"


def build_activity(daily_counts: Counter, today: date) -> dict[str, Any]:
    try:
        last_year_same_day = today.replace(year=today.year - 1)
    except ValueError:
        last_year_same_day = today.replace(year=today.year - 1, day=28)
    start = last_year_same_day + timedelta(days=1)
    end = today
    nonzero = [count for count in daily_counts.values() if count > 0]
    anchor = _percentile_80(nonzero)
    denominator = math.log(anchor + 1) if anchor > 0 else 0.0

    days: list[dict[str, Any]] = []
    cursor = start
    while cursor <= end:
        count = int(daily_counts.get(cursor, 0))
        ratio = 0.0
        if count > 0 and denominator > 0:
            clipped_log = min(math.log(count + 1), anchor * SAFE_BUFFER)
            ratio = min(clipped_log / denominator, 1.0)
        days.append(
            {
                "date": cursor.isoformat(),
                "count": count,
                "ratio": round(ratio, 6),
                "level": _activity_level(ratio),
            }
        )
        cursor += timedelta(days=1)

    first_week = start - timedelta(days=start.weekday())
    last_week = end - timedelta(days=end.weekday())
    day_by_date = {item["date"]: item for item in days}
    weeks = []
    cursor = first_week
    while cursor <= last_week:
        week_days = []
        for offset in range(7):
            current = cursor + timedelta(days=offset)
            week_days.append(day_by_date.get(current.isoformat()))
        weeks.append(
            {
                "week_start": cursor.isoformat(),
                "month": _month_key_for_week(cursor, start, end),
                "days": week_days,
            }
        )
        cursor += timedelta(days=7)

    month_labels = []
    previous = None
    for index, week in enumerate(weeks):
        month = week["month"]
        if month and month != previous:
            month_labels.append({"month": month, "label": f"{int(month[-2:])}月", "week_index": index})
            previous = month

    return {
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "timezone": "Asia/Shanghai",
        "week_starts_on": "monday",
        "user_anchor": round(anchor, 6),
        "safe_buffer": SAFE_BUFFER,
        "colors": ["#ebedf0", "#9be9a8", "#40c463", "#30a14e", "#216e39"],
        "days": days,
        "weeks": weeks,
        "month_labels": month_labels,
    }


def list_recent_learned_skills(profile_name: str, today: date) -> list[dict[str, Any]]:
    skills_root = skills_dir_for_profile(profile_name)
    try:
        result = list_installed(profile_name)
    except Exception:
        logger.debug("profile overview skill listing failed for %s", profile_name, exc_info=True)
        return []
    recent = []
    start = today - timedelta(days=6)
    for skill in result.get("skills", []):
        if not isinstance(skill, dict):
            continue
        full_name = str(skill.get("full_name") or "").strip()
        try:
            skill_dir = (skills_root / full_name).resolve()
            is_user_upload = skill_dir.is_relative_to(skills_root.resolve()) and (
                skill_dir / ".user_created"
            ).is_file()
        except (OSError, ValueError):
            is_user_upload = True
        # "最近学会"只包括 SkillHub 安装和 Agent 在会话中演化生成的技能。
        # WebUI 手工上传用 .user_created 标记；内置系统技能不可删除。
        if is_user_upload or skill.get("can_delete") is False:
            continue
        mtime = read_local_skill_mtime(skills_root, full_name)
        learned_at = _parse_datetime(mtime)
        if learned_at is None or not start <= learned_at.date() <= today:
            continue
        recent.append(
            {
                "name": str(skill.get("name") or ""),
                "display_name": str(skill.get("display_name") or skill.get("name") or ""),
                "description": str(skill.get("display_description") or skill.get("description") or ""),
                "learned_at": _iso_seconds(learned_at),
                "disabled": bool(skill.get("disabled")),
                "source": "skillhub" if skill.get("hub_installed") else "memory_evolution",
            }
        )
    recent.sort(key=lambda item: item["learned_at"] or "", reverse=True)
    return recent[:3]


def raw_file_path(home: Path, file_type: str) -> tuple[str, Path]:
    spec = RAW_FILE_SPECS.get(str(file_type or "").strip().lower())
    if spec is None:
        raise ValueError("不支持的档案类型")
    filename, relative = spec
    return filename, home / Path(relative)


def list_raw_files(home: Path) -> list[dict[str, Any]]:
    files = []
    for file_type, (filename, relative) in RAW_FILE_SPECS.items():
        path = home / Path(relative)
        if not path.is_file():
            continue
        stat = path.stat()
        files.append(
            {
                "file_type": file_type,
                "filename": filename,
                "size": stat.st_size,
                "updated_at": _iso_seconds(datetime.fromtimestamp(stat.st_mtime, tz=SHANGHAI)),
            }
        )
    return files


def build_overview(profile_name: str, *, now: datetime | None = None) -> dict[str, Any]:
    profile, home = resolve_profile(profile_name)
    info = profile.get("info") if isinstance(profile.get("info"), dict) else {}
    current = (now or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    today = current.date()
    try:
        previous_year = today.replace(year=today.year - 1)
    except ValueError:
        previous_year = today.replace(year=today.year - 1, day=28)
    range_start = previous_year + timedelta(days=1)

    created_at = _parse_datetime(info.get("time"))
    work_days = max(0, (today - created_at.date()).days) if created_at else 0
    conversation_count, daily_counts = _collect_sessions(str(profile.get("name") or profile_name), range_start, today)

    return {
        "profile": {
            "name": str(profile.get("name") or profile_name),
            "assistant_name": str(info.get("display_name") or profile.get("name") or profile_name),
            "avatar": str(info.get("logo") or ""),
            "role_type": "general" if profile.get("is_default") else "position",
            "status": "online",
            "created_at": _iso_seconds(created_at),
            "description": str(info.get("description") or ""),
        },
        "metrics": {
            "work_days": work_days,
            "automatic_task_count": _collect_cron_count(home),
            "conversation_task_count": conversation_count,
        },
        "activity": build_activity(daily_counts, today),
        "recent_learned_skills": list_recent_learned_skills(str(profile.get("name") or profile_name), today),
        "raw_files": list_raw_files(home),
    }

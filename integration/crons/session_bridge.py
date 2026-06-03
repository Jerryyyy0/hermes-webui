"""Import cron agent sessions into WebUI sidecar and expose them in the sidebar."""

from __future__ import annotations

import logging
import shutil
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from integration.config import cron_all_profiles_enabled
from integration.crons.listing import (
    _normalize_profile_name,
    _profile_home_for_name,
    resolve_owner_profile_for_job,
)

logger = logging.getLogger(__name__)

# When state.db no longer has the target cron session row, fall back to matching
# owner output .md by the timestamp embedded in cron_<job>_YYYYMMDD_HHMMSS.
CRON_ORPHAN_OUTPUT_MAX_DELTA_SECONDS = 600.0


def cron_sessions_visible_in_sidebar(session: dict) -> bool:
    """Materialized cron sessions (WebUI copy) are visible; raw CLI cron rows stay hidden."""
    if not cron_all_profiles_enabled():
        return False
    source = session.get("source_tag") or session.get("source")
    if source != "cron":
        return False
    return session.get("is_cli_session") is False


def delete_materialized_cron_session_source(
    sid: str,
    *,
    profile_hint: str | None = None,
) -> dict[str, Any]:
    """Best-effort delete for cron sessions so they don't re-materialize.

    Cron sessions are stored in the Hermes Agent SQLite store (state.db) under the
    *execution* profile home. WebUI's /api/session/delete previously deleted only
    the active profile's state.db row, which can miss cron runs and allow later
    materialization to recreate the WebUI sidecar.
    """
    sid = str(sid or "").strip()
    if not sid or not sid.startswith("cron_"):
        return {"ok": False, "deleted": False, "reason": "not_cron_session"}

    deleted_profiles: list[str] = []
    deleted_output_files: list[str] = []
    attempted_profiles: list[str] = []

    try:
        from api.models import _delete_state_db_session_rows
    except Exception:
        return {"ok": False, "deleted": False, "reason": "missing_state_db_delete_helper"}

    job_id = _cron_job_id_from_session_id(sid)
    if not job_id:
        return {"ok": False, "deleted": False, "reason": "invalid_cron_session_id"}

    owner_profiles = _cron_owner_profiles_for_delete(job_id, sid, profile_hint=profile_hint)
    state_db_profiles = _cron_state_db_profiles_for_delete(job_id, sid, profile_hint=profile_hint)
    state_candidates = _cron_session_candidates_for_profiles(state_db_profiles, job_id)

    for profile_name in owner_profiles:
        attempted_profiles.append(profile_name)
        try:
            owner_home = Path(_profile_home_for_name(profile_name))
            output_file = _delete_cron_output_file_for_run(
                owner_home,
                job_id,
                sid,
                candidates=state_candidates,
            )
            if output_file:
                deleted_output_files.append(str(output_file))
        except Exception:
            continue

    for profile_name in state_db_profiles:
        if profile_name not in attempted_profiles:
            attempted_profiles.append(profile_name)
        try:
            db_path = Path(_profile_home_for_name(profile_name)) / "state.db"
            if _delete_state_db_session_rows(db_path, sid):
                deleted_profiles.append(profile_name)
        except Exception:
            continue

    return {
        "ok": True,
        "deleted": bool(deleted_profiles or deleted_output_files),
        "attempted_profiles": attempted_profiles,
        "deleted_profiles": deleted_profiles,
        "deleted_output_files": deleted_output_files,
    }


def delete_cron_job_history(
    job_id: str,
    *,
    owner_profile: str | None = None,
    job: dict | None = None,
) -> dict[str, Any]:
    """Best-effort cleanup for all history attached to one cron job."""
    job_id = str(job_id or "").strip()
    if not job_id:
        return {"ok": False, "deleted": False, "reason": "missing_job_id"}

    owners = _cron_owner_profiles_for_job_delete(job_id, owner_profile=owner_profile)
    state_db_profiles = _cron_state_db_profiles_for_job_delete(
        job_id,
        owner_profile=owner_profile,
        job=job or {},
    )
    session_ids = _cron_session_ids_for_job(state_db_profiles, job_id)
    session_ids.update(_webui_cron_session_ids_for_job(job_id))

    deleted_sidecars: list[str] = []
    deleted_profiles: list[str] = []
    deleted_output_dirs: list[str] = []
    deleted_output_files = 0

    try:
        from api.models import _delete_state_db_session_rows
    except Exception:
        _delete_state_db_session_rows = None

    for sid in sorted(session_ids):
        if _delete_webui_cron_session_sidecar(sid):
            deleted_sidecars.append(sid)
        if _delete_state_db_session_rows is None:
            continue
        for profile_name in state_db_profiles:
            try:
                db_path = Path(_profile_home_for_name(profile_name)) / "state.db"
                if _delete_state_db_session_rows(db_path, sid):
                    marker = f"{profile_name}:{sid}"
                    deleted_profiles.append(marker)
            except Exception:
                continue

    for profile_name in owners:
        try:
            output_dir = Path(_profile_home_for_name(profile_name)) / "cron" / "output" / job_id
            removed_count = _delete_cron_output_dir(output_dir)
            if removed_count is not None:
                deleted_output_dirs.append(str(output_dir))
                deleted_output_files += removed_count
        except Exception:
            continue

    deleted = bool(deleted_sidecars or deleted_profiles or deleted_output_dirs)
    if deleted:
        try:
            from api.session_events import publish_session_list_changed

            publish_session_list_changed("cron_job_delete")
        except Exception:
            pass
    return {
        "ok": True,
        "deleted": deleted,
        "job_id": job_id,
        "owner_profiles": owners,
        "state_db_profiles": state_db_profiles,
        "deleted_session_ids": sorted(session_ids),
        "deleted_sidecars": deleted_sidecars,
        "deleted_profiles": deleted_profiles,
        "deleted_output_dirs": deleted_output_dirs,
        "deleted_output_files": deleted_output_files,
    }


def _cron_owner_profiles_for_job_delete(
    job_id: str,
    *,
    owner_profile: str | None = None,
) -> list[str]:
    owners: list[str] = []
    if isinstance(owner_profile, str) and owner_profile.strip():
        owners.append(owner_profile)
    resolved = resolve_owner_profile_for_job(job_id)
    if resolved:
        owners.append(resolved)
    return _dedupe_profile_names(owners)


def _cron_state_db_profiles_for_job_delete(
    job_id: str,
    *,
    owner_profile: str | None = None,
    job: dict | None = None,
) -> list[str]:
    profiles: list[str] = []
    if isinstance(owner_profile, str) and owner_profile.strip():
        profiles.append(owner_profile)
    resolved = resolve_owner_profile_for_job(job_id)
    if resolved:
        profiles.append(resolved)
    execution_profile = _execution_profile_name(job or {})
    if execution_profile:
        profiles.append(execution_profile)
    try:
        from api.profiles import list_profiles_api

        for row in list_profiles_api() or []:
            if isinstance(row, dict) and row.get("name"):
                profiles.append(str(row.get("name")))
    except Exception:
        profiles.append("default")
    return _dedupe_profile_names(profiles)


def _cron_session_ids_for_job(profile_names: list[str], job_id: str) -> set[str]:
    return {str(row[0]) for row in _cron_session_candidates_for_profiles(profile_names, job_id)}


def _webui_cron_session_ids_for_job(job_id: str) -> set[str]:
    try:
        from api.config import SESSION_DIR
    except Exception:
        return set()

    session_ids: set[str] = set()
    try:
        for path in Path(SESSION_DIR).glob("cron_*.json"):
            if path.name.startswith("_"):
                continue
            sid = path.stem
            if _cron_job_id_from_session_id(sid) == job_id:
                session_ids.add(sid)
    except Exception:
        return session_ids
    return session_ids


def _delete_webui_cron_session_sidecar(sid: str) -> bool:
    try:
        from api.config import LOCK, SESSION_DIR, SESSIONS, _evict_session_agent
        from api.models import is_safe_session_id, prune_session_from_index
    except Exception:
        return False

    sid = str(sid or "").strip()
    if not sid.startswith("cron_") or not is_safe_session_id(sid):
        return False

    deleted = False
    try:
        with LOCK:
            SESSIONS.pop(sid, None)
    except Exception:
        pass
    try:
        _evict_session_agent(sid)
    except Exception:
        pass
    try:
        path = (Path(SESSION_DIR) / f"{sid}.json").resolve()
        path.relative_to(Path(SESSION_DIR).resolve())
        if path.exists():
            deleted = True
        path.unlink(missing_ok=True)
        path.with_suffix(".json.bak").unlink(missing_ok=True)
    except Exception:
        pass
    try:
        prune_session_from_index(sid)
    except Exception:
        pass
    try:
        from api.upload import _session_attachment_dir

        shutil.rmtree(_session_attachment_dir(sid), ignore_errors=True)
    except Exception:
        pass
    return deleted


def _delete_cron_output_dir(output_dir: Path) -> int | None:
    output_dir = Path(output_dir)
    try:
        base = output_dir.parent.resolve()
        target = output_dir.resolve()
        target.relative_to(base)
    except Exception:
        return None
    if not output_dir.exists():
        return None
    if not output_dir.is_dir():
        return None
    try:
        count = len(list(output_dir.glob("*.md")))
        shutil.rmtree(output_dir)
        return count
    except OSError:
        return None


def _cron_session_id_parts(sid: str) -> tuple[str | None, str | None]:
    raw = str(sid or "")
    if not raw.startswith("cron_"):
        return None, None
    body = raw[len("cron_") :]
    # Current shape: cron_<job_id>_YYYYMMDD_HHMMSS. Split the final two
    # timestamp components together so job_id stays ee50... rather than
    # ee50..._YYYYMMDD.
    parts = body.rsplit("_", 2)
    if (
        len(parts) == 3
        and parts[0]
        and len(parts[1]) == 8
        and len(parts[2]) == 6
        and parts[1].isdigit()
        and parts[2].isdigit()
    ):
        return parts[0], f"{parts[1]}_{parts[2]}"
    job_id, sep, timestamp = body.rpartition("_")
    if not sep or not job_id:
        return None, None
    return job_id, timestamp


def _cron_job_id_from_session_id(sid: str) -> str | None:
    job_id, _timestamp = _cron_session_id_parts(sid)
    return job_id


def _cron_run_timestamp_from_session_id(sid: str) -> float | None:
    _job_id, ts_raw = _cron_session_id_parts(sid)
    if not ts_raw:
        return None
    # Current Hermes cron ids use cron_<job_id>_YYYYMMDD_HHMMSS. Treat this as
    # local time to match output file mtimes generated by the same process.
    try:
        import datetime as _dt

        parsed = _dt.datetime.strptime(ts_raw, "%Y%m%d_%H%M%S")
        return parsed.timestamp()
    except (TypeError, ValueError):
        pass
    try:
        return float(ts_raw)
    except (TypeError, ValueError):
        return None


def _dedupe_profile_names(names: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for name in names:
        normalized = _normalize_profile_name(name)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return ordered


def _cron_owner_profiles_for_delete(
    job_id: str,
    sid: str,
    *,
    profile_hint: str | None = None,
) -> list[str]:
    """Profiles whose cron store owns the job output markdown files."""
    owners: list[str] = []
    resolved = resolve_owner_profile_for_job(job_id)
    if resolved:
        owners.append(resolved)
    if isinstance(profile_hint, str) and profile_hint.strip():
        owners.append(profile_hint)
    try:
        from api.models import Session

        meta = Session.load_metadata_only(sid)
        sidecar_profile = getattr(meta, "profile", None) if meta is not None else None
        if isinstance(sidecar_profile, str) and sidecar_profile.strip():
            owners.append(sidecar_profile)
    except Exception:
        pass
    return _dedupe_profile_names(owners)


def _cron_state_db_profiles_for_delete(
    job_id: str,
    sid: str,
    *,
    profile_hint: str | None = None,
) -> list[str]:
    """Profiles that may hold the cron run row in state.db (usually execution home)."""
    profiles: list[str] = []
    if isinstance(profile_hint, str) and profile_hint.strip():
        profiles.append(profile_hint)
    try:
        from api.models import Session

        meta = Session.load_metadata_only(sid)
        sidecar_profile = getattr(meta, "profile", None) if meta is not None else None
        if isinstance(sidecar_profile, str) and sidecar_profile.strip():
            profiles.append(sidecar_profile)
    except Exception:
        pass

    owner = resolve_owner_profile_for_job(job_id)
    if owner:
        profiles.append(owner)
        try:
            from api.profiles import cron_profile_context_for_home
            from cron.jobs import get_job

            with cron_profile_context_for_home(_profile_home_for_name(owner)):
                job = get_job(job_id)
            execution_profile = _execution_profile_name(job or {})
            if execution_profile:
                profiles.append(execution_profile)
        except Exception:
            pass

    try:
        from api.profiles import list_profiles_api

        for row in list_profiles_api() or []:
            if isinstance(row, dict) and row.get("name"):
                profiles.append(str(row.get("name")))
    except Exception:
        profiles.append("default")

    return _dedupe_profile_names(profiles)


def _cron_session_candidates_for_profiles(
    profile_names: list[str],
    job_id: str,
) -> list[tuple[str, str, float | None]]:
    candidates_by_id: dict[str, tuple[str, str, float | None]] = {}
    for profile_name in profile_names:
        try:
            db_path = Path(_profile_home_for_name(profile_name)) / "state.db"
            if not db_path.is_file():
                continue
            with closing(sqlite3.connect(str(db_path))) as conn:
                for row in _cron_session_candidates(conn, job_id):
                    candidates_by_id.setdefault(row[0], row)
        except sqlite3.Error:
            continue
        except Exception:
            continue
    return sorted(
        candidates_by_id.values(),
        key=lambda row: float(row[2] or 0),
        reverse=True,
    )


def _target_sid_in_candidates(
    candidates: list[tuple[str, str, float | None]],
    sid: str,
) -> bool:
    return any(str(row[0]) == sid for row in candidates)


def _find_output_by_timestamp_fallback(
    files: list[Path],
    sid: str,
    *,
    max_delta_seconds: float = CRON_ORPHAN_OUTPUT_MAX_DELTA_SECONDS,
) -> Path | None:
    """Pick the nearest output .md when state.db no longer has the target session row."""
    parsed_ts = _cron_run_timestamp_from_session_id(sid)
    if parsed_ts is None or not files:
        return None
    try:
        target = float(parsed_ts)
    except (TypeError, ValueError):
        return None

    scored: list[tuple[Path, float]] = []
    for path in files:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        scored.append((path, abs(mtime - target)))
    if not scored:
        return None

    chosen, delta = min(scored, key=lambda item: item[1])
    if delta > max_delta_seconds:
        logger.debug(
            "cron orphan output skip: sid=%s nearest=%s delta=%.1fs > %.1fs",
            sid,
            chosen.name,
            delta,
            max_delta_seconds,
        )
        return None
    return chosen


def _find_cron_output_file_for_run(
    owner_home: Path,
    job_id: str,
    *,
    sid: str,
    candidates: list[tuple[str, str, float | None]],
) -> Path | None:
    """Locate one cron output markdown file using the same mtime heuristic as import."""
    output_dir = Path(owner_home) / "cron" / "output" / job_id
    if not output_dir.is_dir():
        return None
    try:
        files = sorted(output_dir.glob("*.md"), key=lambda f: f.stat().st_mtime, reverse=True)
    except OSError:
        return None
    if not files:
        return None

    if candidates and _target_sid_in_candidates(candidates, sid):
        matches: list[tuple[Path, float]] = []
        for path in files:
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            selected = _select_cron_session_candidate(candidates, run_mtime=mtime)
            if selected and selected[0] == sid:
                matches.append((path, mtime))
        if matches:
            target = next((row for row in candidates if row[0] == sid), None)
            try:
                started_at = float(target[2] or 0) if target else 0.0
            except (TypeError, ValueError):
                started_at = 0.0
            chosen, _mtime = min(
                matches,
                key=lambda item: abs(item[1] - started_at) if started_at else item[1],
            )
            return chosen

    # Orphan cleanup: state.db row for this sid is gone (or never visible), but the
    # owner-profile output artifact may still exist and keep /api/crons/history populated.
    return _find_output_by_timestamp_fallback(files, sid)


def _delete_cron_output_file_for_run(
    owner_home: Path,
    job_id: str,
    sid: str,
    *,
    candidates: list[tuple[str, str, float | None]] | None = None,
) -> Path | None:
    """Delete the single cron output file owned by the job storage profile."""
    output_dir = Path(owner_home) / "cron" / "output" / job_id
    if not output_dir.is_dir():
        return None

    chosen = _find_cron_output_file_for_run(
        owner_home,
        job_id,
        sid=sid,
        candidates=candidates or [],
    )
    if chosen is None:
        return None

    try:
        chosen.unlink()
        return chosen
    except OSError:
        return None


def _target_profile_for_job(job: dict, owner_profile: str) -> str:
    from api.routes import _available_cron_profile_names, _normalize_cron_profile_value

    raw = str((job or {}).get("profile") or "").strip()
    if raw:
        try:
            normalized = _normalize_cron_profile_value(raw)
            if normalized:
                return normalized
        except ValueError:
            pass
        if raw in _available_cron_profile_names():
            return raw
    return _normalize_profile_name(owner_profile)


def _execution_profile_name(job: dict) -> str | None:
    raw = str((job or {}).get("profile") or "").strip()
    return raw or None


def _cron_session_candidates(conn, job_id: str) -> list[tuple[str, str, float | None]]:
    pattern = f"cron_{job_id}_%"
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(sessions)")
    cols = {str(row[1]) for row in cur.fetchall()}
    if "source" not in cols:
        return []
    order = "started_at DESC" if "started_at" in cols else "id DESC"
    cur.execute(
        f"""
        SELECT id, title, started_at FROM sessions
        WHERE source = 'cron' AND id LIKE ?
        ORDER BY {order}
        """,
        (pattern,),
    )
    rows = []
    for row in cur.fetchall():
        sid = str(row[0])
        title = str(row[1] or "")
        started = row[2] if len(row) > 2 else None
        rows.append((sid, title, started))
    return rows


def _latest_cron_session_id(conn, job_id: str) -> tuple[str, str, float | None] | None:
    candidates = _cron_session_candidates(conn, job_id)
    return candidates[0] if candidates else None


def _select_cron_session_candidate(
    candidates: list[tuple[str, str, float | None]],
    *,
    run_mtime: float | None = None,
) -> tuple[str, str, float | None] | None:
    if not candidates:
        return None
    if run_mtime is None:
        return candidates[0]

    def _started(row):
        try:
            return float(row[2] or 0)
        except (TypeError, ValueError):
            return 0.0

    # Output files are written after the agent run completes. Prefer the newest
    # cron session that started before that file mtime; fall back to nearest.
    before = [row for row in candidates if _started(row) and _started(row) <= run_mtime + 5]
    if before:
        return max(before, key=_started)
    return min(candidates, key=lambda row: abs((_started(row) or run_mtime) - run_mtime))


def _select_cron_session_for_run(
    conn,
    job_id: str,
    *,
    run_mtime: float | None = None,
) -> tuple[str, str, float | None] | None:
    candidates = _cron_session_candidates(conn, job_id)
    return _select_cron_session_candidate(candidates, run_mtime=run_mtime)


def _cron_output_body(text: str) -> str:
    """Return agent reply body from a cron output markdown file."""
    lines = str(text or "").split("\n")
    response_idx = -1
    for i, line in enumerate(lines):
        if line.startswith("## Response") or line.startswith("# Response"):
            response_idx = i
            break
    if response_idx >= 0:
        return "\n".join(lines[response_idx + 1 :]).strip()
    return "\n".join(lines).strip()


def build_cron_fallback_messages(
    job: dict,
    output_content: str,
    *,
    run_mtime: float | None = None,
) -> list[dict[str, Any]]:
    """Synthetic user/assistant pair when state.db has no cron run messages."""
    prompt = str((job or {}).get("prompt") or "")
    body = _cron_output_body(output_content)
    if not body:
        body = "(Cron completed without output)"
    ts = float(run_mtime) if run_mtime is not None else time.time()
    return [
        {
            "role": "user",
            "content": prompt,
            "timestamp": ts - 1,
            "source": "cron_fallback",
        },
        {
            "role": "assistant",
            "content": body,
            "timestamp": ts,
            "source": "cron_fallback",
        },
    ]


def read_cron_output_for_run(
    job_id: str,
    *,
    run_mtime: float | None = None,
    filename: str | None = None,
) -> tuple[str | None, str | None]:
    """Read a cron run .md file for fallback message materialization."""
    job_id = str(job_id or "").strip()
    if not job_id:
        return None, None
    try:
        from cron.jobs import OUTPUT_DIR as CRON_OUT
    except ImportError:
        return None, None

    out_dir = Path(CRON_OUT) / job_id
    if not out_dir.is_dir():
        return None, None

    if filename:
        path = out_dir / filename
        if path.is_file():
            try:
                return path.read_text(encoding="utf-8", errors="replace"), filename
            except OSError:
                return None, None
        return None, None

    files = sorted(out_dir.glob("*.md"), key=lambda f: f.stat().st_mtime, reverse=True)
    if not files:
        return None, None

    if run_mtime is not None:
        try:
            target = float(run_mtime)
        except (TypeError, ValueError):
            target = None
        if target is not None:
            before = [f for f in files if f.stat().st_mtime <= target + 5]
            if before:
                chosen = max(before, key=lambda f: f.stat().st_mtime)
                try:
                    return chosen.read_text(encoding="utf-8", errors="replace"), chosen.name
                except OSError:
                    return None, None

    try:
        return files[0].read_text(encoding="utf-8", errors="replace"), files[0].name
    except OSError:
        return None, None


def _materialize_cron_session_found(
    job: dict,
    found: tuple[str, str, float | None],
    *,
    target_profile: str,
    execution_profile: str | None,
    fallback_output: str | None = None,
    run_mtime: float | None = None,
) -> str:
    sid, cli_title, started_at = found

    from api.models import Session, ensure_cron_project, import_cli_session
    from api.models import get_state_db_session_messages
    from api.session_events import publish_session_list_changed

    existing = Session.load_metadata_only(sid)
    if existing is not None:
        existing_profile = getattr(existing, "profile", None) or ""
        if _normalize_profile_name(existing_profile) == target_profile:
            needs_update = (
                not getattr(existing, "project_id", None)
                or getattr(existing, "is_cli_session", None) is not False
                or getattr(existing, "source_tag", None) != "cron"
            )
            metadata_count = getattr(existing, "_metadata_message_count", None)
            if not needs_update and (not fallback_output or (metadata_count or 0) > 0):
                return sid

            # load_metadata_only() returns messages=[] by design and Session.save()
            # refuses to persist that stub (#1558). Reload the full session before
            # patching materialized cron metadata so we never wipe transcripts.
            full = Session.load(sid)
            if full is None:
                return sid
            changed = False
            if needs_update:
                if not getattr(full, "project_id", None):
                    full.project_id = ensure_cron_project(profile=target_profile)
                full.is_cli_session = False
                full.source_tag = "cron"
                changed = True
            if not (full.messages or []) and fallback_output:
                full.messages = build_cron_fallback_messages(
                    job,
                    fallback_output,
                    run_mtime=run_mtime,
                )
                changed = True
            if changed:
                full.save()
                publish_session_list_changed("cron_session_imported")
            return sid
        return sid

    msgs = get_state_db_session_messages(sid, profile=execution_profile or target_profile)
    if not msgs:
        msgs = get_state_db_session_messages(sid, profile=target_profile)
    if not msgs and fallback_output:
        msgs = build_cron_fallback_messages(
            job,
            fallback_output,
            run_mtime=run_mtime,
        )

    title = (job or {}).get("name") or cli_title or f"Cron {str((job or {}).get('id') or '').strip()}"
    s = import_cli_session(
        sid,
        title,
        msgs,
        profile=target_profile,
        created_at=started_at,
        updated_at=started_at,
    )
    s.project_id = ensure_cron_project(profile=target_profile)
    s.is_cli_session = False
    s.source_tag = "cron"
    s.save()
    publish_session_list_changed("cron_session_imported")
    return sid


def materialize_cron_session(
    job: dict,
    *,
    owner_profile: str,
    execution_home: Path,
    run_mtime: float | None = None,
    fallback_output: str | None = None,
    fallback_filename: str | None = None,
) -> str | None:
    """Import the latest cron session from execution state.db into target_profile."""
    if not cron_all_profiles_enabled():
        return None
    job_id = str((job or {}).get("id") or "").strip()
    if not job_id:
        return None

    if fallback_output is None and (run_mtime is not None or fallback_filename):
        fallback_output, fallback_filename = read_cron_output_for_run(
            job_id,
            run_mtime=run_mtime,
            filename=fallback_filename,
        )

    owner = _normalize_profile_name(owner_profile)
    target_profile = _target_profile_for_job(job, owner)
    execution_profile = _execution_profile_name(job)

    db_path = Path(execution_home) / "state.db"
    if not db_path.is_file():
        return None

    try:
        with closing(sqlite3.connect(str(db_path))) as conn:
            found = _select_cron_session_for_run(conn, job_id, run_mtime=run_mtime)
    except sqlite3.Error as exc:
        logger.debug("materialize_cron_session: state.db read failed: %s", exc)
        return None

    if not found:
        return None

    return _materialize_cron_session_found(
        job,
        found,
        target_profile=target_profile,
        execution_profile=execution_profile,
        fallback_output=fallback_output,
        run_mtime=run_mtime,
    )


def materialize_cron_sessions_for_runs(
    job: dict,
    *,
    owner_profile: str,
    execution_home: Path,
    runs: list[dict[str, Any]],
) -> dict[str, str]:
    """Batch materialize cron runs and return ``filename -> session_id``.

    History endpoints call this to avoid opening ``state.db`` and scanning all
    candidate cron sessions once per output file.
    """
    if not cron_all_profiles_enabled():
        return {}
    job_id = str((job or {}).get("id") or "").strip()
    if not job_id or not runs:
        return {}

    owner = _normalize_profile_name(owner_profile)
    target_profile = _target_profile_for_job(job, owner)
    execution_profile = _execution_profile_name(job)

    db_path = Path(execution_home) / "state.db"
    if not db_path.is_file():
        return {}

    try:
        with closing(sqlite3.connect(str(db_path))) as conn:
            candidates = _cron_session_candidates(conn, job_id)
    except sqlite3.Error as exc:
        logger.debug("materialize_cron_sessions_for_runs: state.db read failed: %s", exc)
        return {}
    if not candidates:
        return {}

    session_ids: dict[str, str] = {}
    for run in runs:
        filename = str(run.get("filename") or "")
        try:
            run_mtime = run.get("run_mtime")
            if run_mtime is not None:
                run_mtime = float(run_mtime)
        except (TypeError, ValueError):
            run_mtime = None
        found = _select_cron_session_candidate(candidates, run_mtime=run_mtime)
        if not found:
            continue
        sid = _materialize_cron_session_found(
            job,
            found,
            target_profile=target_profile,
            execution_profile=execution_profile,
            fallback_output=run.get("fallback_output"),
            run_mtime=run_mtime,
        )
        if filename:
            session_ids[filename] = sid
    return session_ids


def materialize_cron_session_by_job_id(owner_profile: str, job_id: str) -> str | None:
    """Compensating import when only jobs.json was updated (e.g. external Gateway)."""
    from cron.jobs import get_job

    owner = _normalize_profile_name(owner_profile)
    if not owner:
        owner = resolve_owner_profile_for_job(job_id) or ""
    if not owner:
        return None

    from api.profiles import cron_profile_context_for_home

    with cron_profile_context_for_home(_profile_home_for_name(owner)):
        job = get_job(job_id)
    if not job:
        return None

    from api.routes import _profile_home_for_cron_job

    execution_home = _profile_home_for_cron_job(job)
    return materialize_cron_session(job, owner_profile=owner, execution_home=execution_home)

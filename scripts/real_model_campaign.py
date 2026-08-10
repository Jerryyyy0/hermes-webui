#!/usr/bin/env python3
"""Manual real-model E2E campaign for multi-turn artifact alignment.

Trial questions come either from historical WebUI sessions with stable
write-sourced delivery artifacts, or from the configured model.

Default ``--context-mode first``: only opening-turn prompts, one continuous
plain session per batch (no transcript import / mid-turn replay).

Optional ``--cancel-verify true|false``: when true, the first session is a
cancel-only batch — every turn cancels, with a randomly chosen trigger
(``immediate_after_start`` / ``after_first_tool`` / ``after_manifest_delta`` /
``after_first_artifact``). Artifact alignment is skipped in that batch.
Remaining sessions use the normal sparse cancellation plan.

Optional modes remain available but are not the default:
- replay: mid-turn prompts with transcript prefix via /api/session/import
- mixed: first-turn + replay

This is intentionally outside tests/ and is never run by CI or pytest.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import inspect
import json
import os
import random
import re
import shutil
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_BASE_URL = "http://127.0.0.1:8787"
ROUND_TIMEOUT = 180
SETTLE_TIMEOUT = 30
# Database prompts must come from sufficiently involved turns. Count each
# recorded tool-call event; provider/tool-call IDs are not a deduplication key.
MIN_TOOLS = 10
PREFIX_TOOL_CONTENT_LIMIT = 12000
ContextMode = Literal["mixed", "first", "replay"]
PromptSource = Literal["database", "model"]
WRITE_TOOLS = frozenset({"write_file", "patch", "edit_file", "create_file", "apply_patch", "str_replace"})
DELIVERY_SUFFIXES = frozenset({
    ".md", ".html", ".htm", ".docx", ".doc", ".pptx", ".xlsx", ".csv", ".pdf",
    ".py", ".json", ".yaml", ".yml", ".txt",
})


@dataclass(frozen=True)
class HistoryPrompt:
    source_session_id: str
    title: str
    prompt: str
    turn: int
    turn_key: str
    n_tools: int
    n_write_tools: int
    artifact_paths: tuple[str, ...]
    user_msg_index: int = 0
    source_workspace: str = ""

    @property
    def needs_prefix_replay(self) -> bool:
        return self.turn > 1


CANCEL_TRIGGERS = (
    "immediate_after_start",
    "after_first_tool",
    "after_manifest_delta",
    "after_first_artifact",
)


def cancellation_plan(turns: int) -> dict[int, str]:
    count = min(4, max(1, -(-turns // 4)))
    choices = ((0.20, "immediate_after_start"), (0.40, "after_first_tool"),
               (0.53, "after_manifest_delta"), (0.80, "after_first_artifact"))
    if count == 1:
        choices = choices[:1]
    elif count == 2:
        choices = (choices[0], choices[3])
    elif count == 3:
        choices = (choices[0], choices[1], choices[3])
    result = {}
    for ratio, trigger in choices:
        round_number = max(2, min(turns, round(turns * ratio)))
        while round_number in result and round_number < turns:
            round_number += 1
        result[round_number] = trigger
    return dict(sorted(result.items()))


def random_cancel_verify_plan(turns: int, rng: random.Random) -> dict[int, str]:
    """Every turn cancels; trigger type is chosen randomly (cancel-verify batch)."""
    return {turn: rng.choice(CANCEL_TRIGGERS) for turn in range(1, turns + 1)}


def parse_bool_arg(value: str) -> bool:
    """Parse CLI bool values; do not use ``type=bool`` (``bool("false")`` is True)."""
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    raise argparse.ArgumentTypeError(f"expected bool (true/false), got {value!r}")


def resolve_batch_specs(
    session_count: int,
    turns: int,
    *,
    cancel_verify_session: bool = False,
    rng: random.Random | None = None,
) -> list[dict[str, Any]]:
    """Build batch specs; cancel-verify (if enabled) always occupies the first session."""
    plan = cancellation_plan(turns)
    cancel_rng = rng if rng is not None else random.Random()
    specs: list[dict[str, Any]] = []
    for index in range(session_count):
        batch_index = index + 1
        if cancel_verify_session and index == 0:
            specs.append({
                "batch_index": batch_index,
                "kind": "cancel_verify",
                "cancel_plan": random_cancel_verify_plan(turns, cancel_rng),
            })
            continue
        specs.append({
            "batch_index": batch_index,
            "kind": "normal",
            "cancel_plan": plan,
        })
    return specs


def _state_dir() -> Path:
    home = Path(os.getenv("HERMES_HOME", "~/.hermes")).expanduser()
    return Path(os.getenv("HERMES_WEBUI_STATE_DIR", str(home / "webui"))).expanduser().resolve()


def _is_write_tool(name: str) -> bool:
    n = (name or "").lower()
    return n in WRITE_TOOLS or n.endswith("write_file") or "write_file" in n


def _delivery_path(path: str) -> bool:
    p = (path or "").replace("\\", "/").strip()
    if not p or p.endswith("SKILL.md") or p.startswith("uploads/") or "/uploads/" in p:
        return False
    suffix = PurePosixPath(p).suffix.lower()
    return suffix in DELIVERY_SUFFIXES


def _tool_events(message: dict) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for tc in message.get("tool_calls") or []:
        if not isinstance(tc, dict):
            continue
        tid = tc.get("id") or tc.get("call_id")
        name = ((tc.get("function") or {}).get("name") if isinstance(tc.get("function"), dict) else None) or tc.get("name")
        if tid:
            out.append((str(tid), str(name or "?").lower()))
    content = message.get("content")
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "tool_use" and part.get("id"):
                out.append((str(part["id"]), str(part.get("name") or "?").lower()))
    return out


def _user_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") in (None, "text") and isinstance(part.get("text"), str):
                parts.append(part["text"])
            elif isinstance(part, str):
                parts.append(part)
        return "\n".join(parts)
    return ""


def _noise_prompt(prompt: str, session_id: str) -> bool:
    text = (prompt or "").strip()
    if not text or len(text) < 12:
        return True
    if session_id.startswith("cron_"):
        return True
    if "CAMPAIGN_ID=" in text or "deliverables/turn-" in text:
        return True
    if text.startswith("[IMPORTANT:") or text.startswith("[CONTEXT COMPACTION") or text.startswith("<!DOCTYPE"):
        return True
    if text.startswith("[Your active task list was preserved"):
        return True
    return False


def _load_turn_write_artifacts(manifest_db: Path) -> dict[tuple[str, str], list[str]]:
    """Map (session_id, turn_key) -> write-sourced delivery artifact paths."""
    out: dict[tuple[str, str], list[str]] = defaultdict(list)
    if not manifest_db.is_file():
        return out
    con = sqlite3.connect(f"file:{manifest_db}?mode=ro", uri=True)
    try:
        rows = con.execute(
            """
            SELECT session_id, turn_key, path, COALESCE(source_tool, '')
            FROM session_manifest_records
            WHERE record_kind = 'artifact'
            """
        ).fetchall()
    except sqlite3.Error:
        con.close()
        return out
    con.close()
    for session_id, turn_key, path, source_tool in rows:
        sid = str(session_id or "")
        key = str(turn_key or "")
        src = str(source_tool or "").lower()
        if not sid or not key or not _delivery_path(str(path or "")):
            continue
        if src not in WRITE_TOOLS and "write_file" not in src:
            continue
        out[(sid, key)].append(str(path))
    return out


def load_history_prompt_pool(state_dir: Path | None = None) -> list[HistoryPrompt]:
    """Load candidate prompts from real WebUI history with stable file-generation evidence."""
    root = (state_dir or _state_dir()).expanduser().resolve()
    sessions_dir = root / "sessions"
    manifest_db = root / "session_manifest.db"
    turn_artifacts = _load_turn_write_artifacts(manifest_db)
    if not sessions_dir.is_dir() or not turn_artifacts:
        return []

    pool: list[HistoryPrompt] = []
    seen_prompts: set[str] = set()
    for path in sessions_dir.glob("*.json"):
        if path.name in {"_index.json"} or path.name.endswith(".bak"):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        session_id = str(data.get("session_id") or path.stem)
        messages = data.get("messages") or []
        if not isinstance(messages, list):
            continue
        title = str(data.get("title") or "")[:120]
        index = 0
        turn_no = 0
        while index < len(messages):
            message = messages[index]
            if not isinstance(message, dict) or message.get("role") != "user":
                index += 1
                continue
            turn_no += 1
            prompt = _user_text(message)
            turn_key = str(message.get("_turn_key") or "")
            user_msg_index = index
            cursor = index + 1
            tool_calls: list[tuple[str, str]] = []
            write_count = 0
            while cursor < len(messages):
                row = messages[cursor]
                if isinstance(row, dict) and row.get("role") == "user":
                    break
                if isinstance(row, dict) and row.get("role") == "assistant":
                    for tid, name in _tool_events(row):
                        tool_calls.append((tid, name))
                        if _is_write_tool(name):
                            write_count += 1
                cursor += 1
            index = cursor
            if _noise_prompt(prompt, session_id) or write_count < 1 or len(tool_calls) < MIN_TOOLS:
                continue
            if not turn_key:
                continue
            artifacts = tuple(sorted(set(turn_artifacts.get((session_id, turn_key), []))))
            if not artifacts:
                continue
            dedupe_key = re.sub(r"\s+", "", prompt)[:120]
            if dedupe_key in seen_prompts:
                continue
            seen_prompts.add(dedupe_key)
            pool.append(HistoryPrompt(
                source_session_id=session_id,
                title=title,
                prompt=prompt.strip(),
                turn=turn_no,
                turn_key=turn_key,
                n_tools=len(tool_calls),
                n_write_tools=write_count,
                artifact_paths=artifacts,
                user_msg_index=user_msg_index,
                source_workspace=str(data.get("workspace") or ""),
            ))
    pool.sort(key=lambda item: (-item.n_write_tools, -item.n_tools, item.source_session_id, item.turn))
    return pool


def pool_for_context_mode(pool: list[HistoryPrompt], mode: ContextMode) -> list[HistoryPrompt]:
    if mode == "first":
        return [item for item in pool if not item.needs_prefix_replay]
    if mode == "replay":
        return [item for item in pool if item.needs_prefix_replay]
    return list(pool)


def _looks_like_control_user_message(content: str) -> bool:
    text = (content or "").lstrip()
    return text.startswith("[Your active task list") or text.startswith("[CONTEXT COMPACTION") or text.startswith("[IMPORTANT:")


def trim_incomplete_prefix_tail(messages: list[dict]) -> list[dict]:
    """Drop a trailing assistant row that still awaits tool results.

    Cutting the transcript at the next user message usually lands on a clean
    boundary, but interrupted turns can leave ``assistant + tool_calls`` as the
    final prefix row. Importing that shape confuses replay / model context.
    """
    out = list(messages)
    while out:
        last = out[-1]
        if not isinstance(last, dict):
            out.pop()
            continue
        if last.get("role") == "assistant" and last.get("tool_calls"):
            out.pop()
            continue
        break
    return out


def sanitize_prefix_messages(messages: list[Any]) -> list[dict]:
    """Prepare imported prefix messages for /api/session/import.

    Preserve/assign stable ``_turn_key`` on real user rows. Stripping keys and
    then appending a keyed campaign prompt creates a mixed keyed/unkeyed
    transcript; WebUI turn alignment then ignores all unkeyed history, so the
    UI first paint only shows the latest campaign turn.
    """
    cleaned: list[dict] = []
    import_turn = 0
    for raw in messages:
        if not isinstance(raw, dict):
            continue
        row = {k: v for k, v in raw.items() if k != "_partial_tool_calls"}
        content = row.get("content")
        if isinstance(content, str) and len(content) > PREFIX_TOOL_CONTENT_LIMIT:
            row["content"] = content[:PREFIX_TOOL_CONTENT_LIMIT] + "\n…[truncated for campaign replay]…"
            content = row["content"]
        if row.get("role") == "user":
            key = str(row.get("_turn_key") or "").strip()
            text = content if isinstance(content, str) else ""
            if not key and not _looks_like_control_user_message(text):
                row["_turn_key"] = f"import:{import_turn}"
                import_turn += 1
        cleaned.append(row)
    return trim_incomplete_prefix_tail(cleaned)


def load_prefix_messages(state_dir: Path, question: HistoryPrompt) -> list[dict]:
    path = state_dir / "sessions" / f"{question.source_session_id}.json"
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    messages = data.get("messages") if isinstance(data, dict) else None
    if not isinstance(messages, list):
        return []
    end = max(0, min(question.user_msg_index, len(messages)))
    return sanitize_prefix_messages(messages[:end])


def _turn_key_user_indexes(messages: list[Any]) -> dict[str, int]:
    """Map each ``_turn_key`` to the index of its first user message."""
    out: dict[str, int] = {}
    for index, message in enumerate(messages):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        key = str(message.get("_turn_key") or "").strip()
        if key and key not in out:
            out[key] = index
    return out


def prefix_delivery_paths(state_dir: Path, question: HistoryPrompt) -> list[str]:
    """Return write-sourced delivery paths from turns *before* the sampled prompt.

    Must not include the sampled turn (or later turns): those files are the
    historical answers and would leak the expected delivery into the replay
    workspace.
    """
    path = state_dir / "sessions" / f"{question.source_session_id}.json"
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    messages = data.get("messages") if isinstance(data, dict) else None
    if not isinstance(messages, list):
        return []
    key_to_idx = _turn_key_user_indexes(messages)
    cutoff = max(0, question.user_msg_index)
    paths: list[str] = []
    for (sid, turn_key), values in _load_turn_write_artifacts(state_dir / "session_manifest.db").items():
        if sid != question.source_session_id:
            continue
        idx = key_to_idx.get(turn_key)
        if idx is None or idx >= cutoff:
            continue
        paths.extend(values)
    return sorted(set(paths))


def copy_replay_files(source_workspace: str, destination: Path, relative_paths: list[str]) -> list[str]:
    copied: list[str] = []
    if not source_workspace:
        return copied
    src_root = Path(source_workspace).expanduser()
    if not src_root.is_dir():
        return copied
    for relative in relative_paths:
        rel = str(relative or "").replace("\\", "/").lstrip("/")
        if not rel or ".." in PurePosixPath(rel).parts:
            continue
        src = (src_root / rel).resolve()
        try:
            src.relative_to(src_root.resolve())
        except ValueError:
            continue
        if not src.is_file() or src.is_symlink():
            continue
        dst = destination / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied.append(rel)
    return copied


CLARIFY_AUTO_REPLY = "按你的最佳判断继续执行，无需再向我确认。"


def enable_auto_approve(api: "Api", session_id: str) -> dict[str, Any]:
    """Enable session YOLO so dangerous-tool approvals do not block the campaign."""
    if not session_id:
        return {"ok": False, "error": "missing session_id"}
    return api.request("POST", "/api/session/yolo", {"session_id": session_id, "enabled": True})


def drain_blocking_prompts(api: "Api", session_id: str, *, limit: int = 8) -> dict[str, int]:
    """Resolve pending approval/clarify prompts so the agent does not sit idle."""
    counts = {"approvals": 0, "clarifies": 0}
    if not session_id:
        return counts
    for _ in range(limit):
        pending = api.request(
            "GET",
            "/api/approval/pending?session_id=" + urllib.parse.quote(session_id),
        )
        item = pending.get("pending") if isinstance(pending, dict) else None
        if not isinstance(item, dict):
            break
        api.request("POST", "/api/approval/respond", {
            "session_id": session_id,
            "choice": "session",
            "approval_id": item.get("approval_id") or item.get("id") or "",
        })
        counts["approvals"] += 1
    for _ in range(limit):
        pending = api.request(
            "GET",
            "/api/clarify/pending?session_id=" + urllib.parse.quote(session_id),
        )
        item = pending.get("pending") if isinstance(pending, dict) else None
        if not isinstance(item, dict):
            break
        api.request("POST", "/api/clarify/respond", {
            "session_id": session_id,
            "clarify_id": item.get("clarify_id") or item.get("id") or "",
            "response": CLARIFY_AUTO_REPLY,
        })
        counts["clarifies"] += 1
    return counts


def create_plain_session(
    api: Api,
    *,
    title_prefix: str = "campaign-first",
) -> tuple[str, Path | None]:
    """Create a campaign session in the server-managed workspace."""
    created = api.request("POST", "/api/session/new", {"worktree": False})
    session = created.get("session", {}) if isinstance(created, dict) else {}
    session_id = str(session.get("session_id") or "")
    workspace_text = str(session.get("workspace") or "").strip()
    try:
        workspace = Path(workspace_text).expanduser().resolve() if workspace_text else None
    except OSError:
        workspace = None
    if session_id:
        api.request("POST", "/api/session/rename", {
            "session_id": session_id,
            "title": f"{title_prefix}:{session_id}",
        })
        enable_auto_approve(api, session_id)
    return session_id, workspace


def _is_campaign_test_session(session: dict, *, campaigns_root: Path) -> bool:
    """True when a WebUI session belongs to real_model_campaign test data."""
    title = str(session.get("title") or "")
    workspace = str(session.get("workspace") or "")
    if title.startswith("campaign-replay:") or title.startswith("campaign-first:"):
        return True
    if "e2e_campaigns" not in workspace.replace("\\", "/"):
        return False
    try:
        Path(workspace).expanduser().resolve().relative_to(campaigns_root.resolve())
        return True
    except (OSError, ValueError):
        return "e2e_campaigns" in workspace.replace("\\", "/")


def list_campaign_test_sessions(state_dir: Path | None = None) -> list[dict[str, str]]:
    """List campaign test sessions (id/title/workspace) under the WebUI state dir."""
    root = (state_dir or _state_dir()).expanduser().resolve()
    campaigns_root = root / "e2e_campaigns"
    sessions_dir = root / "sessions"
    out: list[dict[str, str]] = []
    if not sessions_dir.is_dir():
        return out
    for path in sorted(sessions_dir.glob("*.json")):
        if path.name.startswith("_") or path.name.endswith(".bak"):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict) or not _is_campaign_test_session(data, campaigns_root=campaigns_root):
            continue
        sid = str(data.get("session_id") or path.stem)
        out.append({
            "session_id": sid,
            "title": str(data.get("title") or "")[:120],
            "workspace": str(data.get("workspace") or ""),
            "workspace_mode": str(data.get("workspace_mode") or ""),
        })
    return out


def cleanup_campaign_test_data(api: "Api | None" = None, state_dir: Path | None = None) -> dict[str, Any]:
    """Delete campaign sessions and their external or managed workspaces.

    ``/api/session/delete`` does not remove workspaces. Managed roots are only
    removed after their marked campaign session was deleted successfully.
    """
    root = (state_dir or _state_dir()).expanduser().resolve()
    campaigns_root = root / "e2e_campaigns"
    sessions = list_campaign_test_sessions(root)
    deleted: list[str] = []
    deleted_session_ids: set[str] = set()
    failed: list[dict[str, Any]] = []
    client = api
    if client is None and sessions:
        client = Api(DEFAULT_BASE_URL)
    for row in sessions:
        sid = row["session_id"]
        try:
            if client is None:
                raise RuntimeError("api client unavailable")
            result = client.request("POST", "/api/session/delete", {"session_id": sid})
            if not (isinstance(result, dict) and result.get("ok") is True):
                failed.append({"session_id": sid, "error": result})
                continue
            deleted.append(sid)
            deleted_session_ids.add(sid)
        except Exception as exc:
            failed.append({"session_id": sid, "error": str(exc)})

    removed_campaign_dirs: list[str] = []
    if campaigns_root.is_dir():
        for child in sorted(campaigns_root.iterdir()):
            if child.name.startswith("."):
                continue
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=False)
                removed_campaign_dirs.append(child.name)
            else:
                child.unlink(missing_ok=True)
                removed_campaign_dirs.append(child.name)
    else:
        campaigns_root.mkdir(parents=True, exist_ok=True)

    removed_managed_workspaces: list[str] = []
    for row in sessions:
        if row["session_id"] not in deleted_session_ids:
            continue
        if not str(row.get("title") or "").startswith("campaign-first:"):
            continue
        if str(row.get("workspace_mode") or "").strip().lower() != "managed":
            continue
        try:
            workspace = Path(str(row.get("workspace") or "")).expanduser().resolve()
            if workspace.name != row["session_id"] or workspace.parent.name != "sessions":
                continue
            shutil.rmtree(workspace, ignore_errors=False)
            removed_managed_workspaces.append(str(workspace))
        except (OSError, ValueError):
            failed.append({"session_id": row["session_id"], "error": "managed workspace cleanup failed"})

    return {
        "ok": not failed,
        "deleted_sessions": deleted,
        "failed_sessions": failed,
        "removed_campaign_dirs": removed_campaign_dirs,
        "removed_managed_workspaces": removed_managed_workspaces,
        "campaigns_root": str(campaigns_root),
    }


def create_replay_session(
    api: Api,
    state_dir: Path,
    workspace: Path,
    question: HistoryPrompt,
    model: str,
) -> tuple[str, dict[str, Any], Path | None]:
    meta: dict[str, Any] = {"strategy": "replay_prefix", "prefix_messages": 0, "copied_files": []}
    prefix = load_prefix_messages(state_dir, question)
    meta["prefix_messages"] = len(prefix)
    if not prefix:
        meta["fallback"] = "missing_prefix_use_plain_session"
        session_id, managed_workspace = create_plain_session(api)
        return session_id, meta, managed_workspace
    paths = prefix_delivery_paths(state_dir, question)
    meta["copied_files"] = copy_replay_files(
        question.source_workspace,
        workspace,
        paths,
    )
    imported = api.request("POST", "/api/session/import", {
        "title": f"campaign-replay:{question.source_session_id}:turn{question.turn}",
        "workspace": str(workspace),
        "model": model,
        "messages": prefix,
    })
    session_id = str(imported.get("session", {}).get("session_id") or "")
    if not session_id:
        meta["fallback"] = "import_failed_use_plain_session"
        meta["import_error"] = imported.get("error") or imported
        session_id, managed_workspace = create_plain_session(api)
        return session_id, meta, managed_workspace
    enable_auto_approve(api, session_id)
    return session_id, meta, workspace


def seed_workspace(workspace: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "constraints.md").write_text(
        "请在工作区创建或修改真实文件完成交付；不要只在回复中描述结果；不要删除用户未要求删除的文件。\n",
        encoding="utf-8",
    )
    (workspace / "deliverables").mkdir(parents=True, exist_ok=True)


def model_campaign_phase(turn: int) -> tuple[str, str]:
    """Return the model-campaign phase's requested business file and instructions."""
    if turn < 1:
        raise ValueError("campaign turn must be positive")
    if turn == 1:
        return "data/source.csv", """第 1 阶段：根据场景构造可用的源数据，并创建 `data/source.csv`。
数据必须包含后续分析需要的字段、多个记录，以及与场景相关的至少一个异常或边界情况。"""
    if turn == 2:
        return "report/analysis.md", """第 2 阶段：先检查 `data/source.csv`；若前序轮因取消未完成，可补建必要的前序文件。
完成数据清洗与分析，并创建 `report/analysis.md`，说明口径、发现、异常处理和可执行结论。"""
    if turn == 3:
        return "dashboard/index.html", """第 3 阶段：先检查已有数据和分析结果；若前序轮因取消未完成，可补建必要的前序文件。
创建 `dashboard/index.html`，用真实场景数据呈现关键指标、趋势或分组比较，并体现分析结论。"""

    cycle = (turn - 4) % 3
    if cycle == 0:
        target = f"review/iteration-{turn:02d}.md"
        kind = "审查说明，记录本轮核验、发现的问题和对已有交付的改进"
    elif cycle == 1:
        target = f"data/derived-{turn:02d}.csv"
        kind = "衍生数据集，补充可复算的指标、分组或质量标记"
    else:
        target = f"dashboard/iteration-{turn:02d}.html"
        kind = "页面迭代，补充一个能帮助用户判断或比较的可视化视图"
    return target, f"""第 {turn} 阶段：检查并利用此前已完成的工作；若前序轮因取消未完成，可补建必要的前序文件。
创建 `{target}`，作为{kind}。不要只在回复中描述结果。"""


def build_history_prompt(
    question: HistoryPrompt,
    turn: int,
    nonce: str,
    *,
    model_campaign: bool = False,
) -> tuple[str, str]:
    if model_campaign:
        _target, phase = model_campaign_phase(turn)
        prompt = f"""{question.prompt}

这是同一业务场景的连续多轮交付。{phase}
请在工作区创建或修改真实文件完成本轮工作，不要只提供文字建议或结果摘要。
完成后简要说明实际创建或修改的文件。
CAMPAIGN_ID={{campaign_id}}
SESSION_ID={{session_id}}
TURN={turn}
NONCE={nonce}"""
        # The planned path guides the model only. Alignment accepts any real
        # artifact the manifest attributes to this turn.
        return prompt, ""

    artifact = f"deliverables/turn-{turn:02d}/delivery.md"
    prompt = f"""{question.prompt}

请基于上述真实历史需求完成本轮工作，并在工作区创建或修改实际文件（不要只描述结果）。
优先把面向用户的主要交付写到 `{artifact}`；若需求明确要求 html/md/docx 等其它路径，也可额外生成。
主要交付文件中必须包含以下标记行：
CAMPAIGN_ID={{campaign_id}}
SESSION_ID={{session_id}}
TURN={turn}
NONCE={nonce}
完成后简要说明实际创建或修改的文件。"""
    return prompt, artifact


def pick_history_prompt(pool: list[HistoryPrompt], rng: random.Random, used: set[str] | None = None) -> HistoryPrompt:
    if not pool:
        raise RuntimeError("history prompt pool is empty")
    unused = [item for item in pool if item.source_session_id + ":" + item.turn_key not in (used or set())]
    choice = rng.choice(unused or pool)
    if used is not None:
        used.add(choice.source_session_id + ":" + choice.turn_key)
    return choice


_MODEL_PROMPT_GENERATION_REQUEST = """你是 Hermes WebUI 的 E2E 测试题目设计器。
请生成 {count} 条彼此不同的中文业务场景，用于同一会话中的连续多轮文件交付测试。
每条场景必须明确写出“生成”或“创建”一个具体类型的“文件”（例如“生成 Markdown 文件”“创建 HTML 文件”“生成 CSV 文件”），并给出足够具体的业务背景、数据对象、使用者和交付约束。场景应适合后续跨格式地依次产出 CSV 数据、Markdown 分析和 HTML 页面，并包含至少一个可变复杂约束，例如异常处理、字段映射、版本对比或验证清单。不得只要求分析、回答或提供建议。不要规定绝对路径、不要包含 CAMPAIGN_ID、不要要求联网、不要要求删除文件。
不要执行任务、不要解释、不要使用工具。只返回 JSON 字符串数组，例如：["需求一", "需求二"]。"""


def _generated_prompt_texts(response_text: str) -> list[str]:
    """Parse the generator's strict JSON-array response without guessing prose."""
    text = str(response_text or "").strip()
    candidates = [text]
    fenced = re.findall(r"```(?:json)?\s*(\[.*?\])\s*```", text, flags=re.IGNORECASE | re.DOTALL)
    candidates.extend(fenced)
    start, end = text.find("["), text.rfind("]")
    if start >= 0 and end > start:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            values = json.loads(candidate)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(values, list):
            continue
        prompts: list[str] = []
        seen: set[str] = set()
        for value in values:
            if not isinstance(value, str):
                continue
            prompt = value.strip()
            key = re.sub(r"\s+", "", prompt)
            if (
                not key
                or not re.search(r"(?:生成|创建).{0,80}文件", prompt)
                or key in seen
                or _noise_prompt(prompt, "model-generator")
            ):
                continue
            seen.add(key)
            prompts.append(prompt)
        if prompts:
            return prompts
    raise RuntimeError("model prompt generator returned no valid JSON prompt array")


def _model_prompt_generation_error(exc: Exception) -> str:
    """Summarize an auxiliary-call failure without exposing upstream details."""
    if isinstance(exc, ModuleNotFoundError):
        return "Hermes Agent auxiliary runtime unavailable; check HERMES_WEBUI_AGENT_DIR and its dependencies"
    status = getattr(exc, "status_code", None)
    if not isinstance(status, int):
        status = getattr(getattr(exc, "response", None), "status_code", None)
    if status in {401, 403}:
        return (
            f"model prompt generation authentication failed (HTTP {status}); "
            "check the current profile's model credentials"
        )
    if status == 404:
        return "model prompt generation endpoint or model was not found (HTTP 404); check the current profile's model route"
    if isinstance(status, int):
        return f"model prompt generation failed with HTTP {status}; check the current profile's model route"
    return "model prompt generation failed; check the current profile's model endpoint and credentials"


def _call_llm_accepts_api_mode(call_llm: Any) -> bool:
    """Keep direct campaign calls compatible with older Hermes Agent runtimes."""
    try:
        parameters = inspect.signature(call_llm).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.name == "api_mode" or parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )


def generate_model_prompt_pool(
    model: str,
    count: int,
    *,
    model_config: dict[str, Any] | str | None = None,
) -> list[HistoryPrompt]:
    """Generate multi-turn campaign scenarios without creating a WebUI session."""
    try:
        from api.config import _AGENT_DIR
        from api.profiles import get_active_profile_name, get_hermes_home_for_profile, profile_env_for_background_worker
        from integration.assistant_bubbles.collectors import model_route

        profile = get_active_profile_name()
        route = model_route(get_hermes_home_for_profile(profile))
        agent_dir = str(_AGENT_DIR or "").strip()
        if agent_dir and agent_dir not in sys.path:
            sys.path.insert(0, agent_dir)
        auxiliary_client = importlib.import_module("agent.auxiliary_client")
        call_llm = getattr(auxiliary_client, "call_llm")
        with profile_env_for_background_worker(profile, purpose="campaign prompt generation"):
            if not isinstance(model_config, dict):
                model_config = {"default": model_config}
            configured_model = str(model_config.get("default") or "").strip()
            resolved_model = str(model or configured_model or route.get("model") or "").strip()
            if not resolved_model:
                raise RuntimeError("configured default model is empty")
            configured_provider = str(model_config.get("provider") or "").strip() or None
            configured_base_url = str(model_config.get("base_url") or "").strip() or None
            if configured_base_url:
                provider = configured_provider or "custom"
            else:
                provider = configured_provider or route.get("provider")
            call_kwargs: dict[str, Any] = {
                "task": "campaign_prompt_generation",
                "provider": provider,
                "model": resolved_model,
                "base_url": configured_base_url or route.get("base_url"),
                "api_key": str(model_config.get("api_key") or model_config.get("api") or "").strip() or None,
                "messages": [
                    {"role": "system", "content": "你只负责生成测试题目，不执行题目中的工作，也不调用工具。"},
                    {"role": "user", "content": _MODEL_PROMPT_GENERATION_REQUEST.format(count=max(1, count))},
                ],
                "temperature": 0.4,
                "max_tokens": max(256, min(4096, max(1, count) * 160)),
                "timeout": 60,
            }
            api_mode = str(model_config.get("api_mode") or "").strip() or None
            if api_mode and _call_llm_accepts_api_mode(call_llm):
                call_kwargs["api_mode"] = api_mode
            response = call_llm(
                **call_kwargs,
            )
        response_text = response.choices[0].message.content
    except Exception as exc:
        raise RuntimeError(_model_prompt_generation_error(exc)) from None

    prompts = _generated_prompt_texts(response_text)
    requested = max(1, count)
    if len(prompts) < requested:
        raise RuntimeError(
            f"model prompt generator returned {len(prompts)} scenarios; {requested} required"
        )
    prompts = prompts[:requested]
    return [
        HistoryPrompt(
            source_session_id="model-generated",
            title="model-generated",
            prompt=prompt,
            turn=1,
            turn_key=f"generated:{index}",
            n_tools=0,
            n_write_tools=0,
            artifact_paths=(),
        )
        for index, prompt in enumerate(prompts, start=1)
    ]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


class Api:
    def __init__(self, base_url: str): self.base_url = base_url.rstrip("/")

    def request(self, method: str, path: str, body: dict | None = None, timeout: int = 20) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base_url + path, data=data, method=method, headers={"Content-Type": "application/json"} if data else {})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            try: payload = json.loads(exc.read().decode())
            except json.JSONDecodeError: payload = {"error": str(exc)}
            payload["_http_status"] = exc.code
            return payload

    def open_event_stream(self, stream_id: str):
        request = urllib.request.Request(self.base_url + "/api/chat/stream?stream_id=" + urllib.parse.quote(stream_id))
        return urllib.request.urlopen(request, timeout=ROUND_TIMEOUT)

    def events(self, stream_id: str):
        with self.open_event_stream(stream_id) as response:
            event, data = "message", []
            for raw in response:
                line = raw.decode(errors="replace").strip()
                if not line and data:
                    try: yield event, json.loads("\n".join(data))
                    except json.JSONDecodeError: yield event, {"raw": "\n".join(data)}
                    event, data = "message", []
                elif line.startswith("event:"): event = line[6:].strip()
                elif line.startswith("data:"): data.append(line[5:].strip())


def _message_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    try:
        return json.dumps(content, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(content)


def _turn_key(session: dict, nonce: str) -> str:
    """Resolve turn_key from the campaign user prompt only.

    Tool/assistant rows often echo NONCE while writing files; scanning all roles
    makes the old exact-one-match check fail even when the user turn is fine.
    """
    rows = [
        m for m in session.get("messages", [])
        if isinstance(m, dict) and m.get("role") == "user" and nonce in _message_text(m)
    ]
    if len(rows) != 1:
        return ""
    return str(rows[0].get("_turn_key") or "")


def _artifacts(manifest: dict, turn_key: str) -> set[str]:
    row = next((r for r in manifest.get("turns", []) if r.get("turn_key") == turn_key), {})
    return {str(item.get("path") or "") for item in row.get("artifacts", []) if item.get("path")}


def _resolve_manifest_artifact_path(workspace: Path, raw_path: str) -> Path | None:
    """Resolve a manifest wire path to a file inside the session workspace.

    The session Manifest API projects managed-session paths onto the trusted
    integration root (for example ``sessions/<sid>/data/source.csv``), while
    the campaign receives the session-specific workspace (``.../sessions/<sid>``).
    Accept both wire shapes, but only after the resolved candidate is contained
    by the session workspace.
    """
    text = str(raw_path or "").strip().replace("\\", "/")
    if not text:
        return None
    session_root = workspace.expanduser().resolve()
    raw = Path(text).expanduser()
    candidates: list[Path] = []
    if raw.is_absolute():
        candidates.append(raw)
    else:
        try:
            from api.workspace import resolve_trusted_workspace

            candidates.append(resolve_trusted_workspace() / raw)
        except (ImportError, OSError, RuntimeError, ValueError):
            pass
        candidates.append(session_root / raw)
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
            resolved.relative_to(session_root)
        except (OSError, RuntimeError, ValueError):
            continue
        if candidate.is_symlink():
            continue
        return resolved
    return None


def _is_primary_delivery(path: str) -> bool:
    name = PurePosixPath(str(path or "").replace("\\", "/")).name
    return name == "delivery.md"


def evaluate_alignment(session: dict, manifest: dict, ledger: dict, workspace: Path) -> tuple[list[dict], list[dict], dict[str, str]]:
    failures, observations, hashes = [], [], {}
    actual_key = _turn_key(session, ledger["nonce"])
    if not actual_key:
        return failures, [{"code": "TURN_KEY_UNAVAILABLE"}], hashes
    ledger["turn_key"] = actual_key
    turn_keys = {str(row.get("turn_key") or "") for row in manifest.get("turns", [])}
    if actual_key not in turn_keys: failures.append({"code": "MANIFEST_TURN_MISSING", "turn_key": actual_key})
    if actual_key in set(manifest.get("diagnostics", {}).get("orphan_turn_keys", [])): failures.append({"code": "MANIFEST_ORPHAN_TURN", "turn_key": actual_key})
    artifacts = _artifacts(manifest, actual_key)
    expected = str(ledger.get("expected_artifact_path") or "")
    if expected and expected not in artifacts:
        observations.append({"code": "EXPECTED_DELIVERY_MISSING", "path": expected})
    for relative in artifacts:
        path = _resolve_manifest_artifact_path(workspace, relative)
        if path is None or not path.is_file():
            failures.append({"code": "ARTIFACT_PATH_INVALID", "path": relative}); continue
        # Only the primary campaign delivery must embed NONCE; companion html/png
        # under deliverables/ are allowed without the marker.
        if expected and relative == expected and _is_primary_delivery(relative) and ledger["nonce"] not in path.read_text(encoding="utf-8", errors="replace"):
            failures.append({"code": "ARTIFACT_NONCE_MISMATCH", "path": relative})
        hashes[relative] = _sha256(path)
        # Ownership conflicts matter for campaign outputs, not seed/prose noise
        # like constraints.md attributed across multiple turns.
        if relative.startswith("deliverables/") or relative.startswith("work/"):
            for other in manifest.get("turns", []):
                other_paths = {str(a.get("path") or "") for a in other.get("artifacts", [])}
                if other.get("turn_key") != actual_key and relative in other_paths:
                    failures.append({"code": "ARTIFACT_MULTI_TURN_OWNER", "path": relative, "other_turn": other.get("turn_key")})
    if not artifacts:
        failures.append({"code": "MODEL_NO_ARTIFACT"})
    return failures, observations, hashes


def _persist_campaign_summary(path: Path, summary: dict[str, Any], *, completed: bool) -> None:
    payload = dict(summary)
    payload["completed"] = completed
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _wait_for_chat_ready(api: Api, session_id: str) -> dict:
    """Wait until the server confirms a successor turn cannot race teardown."""
    until, latest = time.monotonic() + SETTLE_TIMEOUT, {}
    while time.monotonic() < until:
        latest = api.request(
            "GET",
            "/api/session/status?session_id=" + urllib.parse.quote(session_id),
        )
        if latest.get("can_start_chat") is True:
            return latest
        time.sleep(1.0)
    return latest


def _settle(api: Api, session_id: str) -> tuple[dict, dict]:
    readiness = _wait_for_chat_ready(api, session_id)
    if readiness.get("can_start_chat") is not True:
        return readiness, {}
    session = api.request("GET", "/api/session?session_id=" + urllib.parse.quote(session_id) + "&messages=1")
    return readiness, session


def _cancel_accepted(payload: dict | None) -> bool:
    return isinstance(payload, dict) and payload.get("ok") is True and payload.get("cancelled") is True


def _dispatch_cancel(api: Api, stream_id: str) -> tuple[dict, float]:
    t0 = time.monotonic()
    payload = api.request("GET", "/api/chat/cancel?stream_id=" + urllib.parse.quote(stream_id))
    return payload, round((time.monotonic() - t0) * 1000, 1)


def _trigger_matched(trigger: str | None, event: str, payload: dict) -> bool:
    if trigger == "after_first_tool":
        return event in {"tool", "tool_complete"}
    if trigger == "after_manifest_delta":
        return event == "manifest_delta"
    if trigger == "after_first_artifact":
        return event == "manifest_delta" and bool(payload.get("artifacts"))
    return False


def _run_round(
    api: Api,
    session_id: str,
    workspace: Path,
    question: HistoryPrompt,
    campaign_id: str,
    turn: int,
    trigger: str | None,
    *,
    start_ready: bool = False,
    cancel_only: bool = False,
    model_campaign: bool = False,
) -> dict:
    nonce = uuid.uuid4().hex
    prompt, expected = build_history_prompt(question, turn, nonce, model_campaign=model_campaign)
    prompt = prompt.format(campaign_id=campaign_id, session_id=session_id)
    row: dict[str, Any] = {
        "turn": turn,
        "nonce": nonce,
        "prompt": prompt,
        "history_prompt": asdict(question),
        "expected_artifact_path": expected,
        "planned_artifact_path": model_campaign_phase(turn)[0] if model_campaign else "",
        "cancel_trigger": trigger,
        "cancel_only": cancel_only,
        "events": [],
        "observations": [],
        "alignment_failures": [],
        "auto_approve": {"yolo": None, "drains": []},
        "cancelled": False,
    }
    readiness = {"can_start_chat": True, "source": "known_ready"} if start_ready else _wait_for_chat_ready(api, session_id)
    row["readiness_before_start"] = readiness
    if readiness.get("can_start_chat") is not True:
        row["observations"].append({"code": "SESSION_NOT_READY"})
        row["alignment_failures"].append({"code": "SESSION_NOT_READY"})
        return row
    row["auto_approve"]["yolo"] = enable_auto_approve(api, session_id)
    start = api.request("POST", "/api/chat/start", {"session_id": session_id, "workspace": str(workspace), "message": prompt})
    chat_start_received_at = time.monotonic()
    row["chat_start"], row["stream_id"] = start, str(start.get("stream_id") or "")
    if not row["stream_id"]:
        row["observations"].append({"code": "CHAT_START_FAILED", "detail": start.get("error", "missing stream_id")}); return row
    cancelled = False
    if trigger == "immediate_after_start":
        # Confirm the SSE endpoint accepts the stream before exercising cancellation.
        stream_connection = None
        try:
            stream_connection = api.open_event_stream(row["stream_id"])
            row["stream_opened_for_cancel"] = True
        except (OSError, TimeoutError, urllib.error.URLError) as exc:
            row["stream_opened_for_cancel"] = False
            row["observations"].append({"code": "STREAM_OPEN_ERROR", "detail": type(exc).__name__})
        try:
            row["cancel_dispatch_delay_ms"] = round((time.monotonic() - chat_start_received_at) * 1000, 1)
            row["cancel"], row["cancel_response_ms"] = _dispatch_cancel(api, row["stream_id"])
            cancelled = True
            if row["cancel_dispatch_delay_ms"] > 250:
                row["alignment_failures"].append({"code": "IMMEDIATE_CANCEL_SLOW", "actual_ms": row["cancel_dispatch_delay_ms"]})
            if not _cancel_accepted(row["cancel"]):
                row["observations"].append({"code": "CANCEL_NOT_ACCEPTED", "detail": row["cancel"]})
                if cancel_only:
                    row["alignment_failures"].append({"code": "CANCEL_NOT_ACCEPTED", "detail": row["cancel"]})
        finally:
            if stream_connection is not None:
                stream_connection.close()
    else:
        try:
            for event, payload in api.events(row["stream_id"]):
                row["events"].append({"event": event, "payload": payload})
                # Drain only when the stream signals a blocking prompt; avoid
                # polling /api/approval|clarify/pending on every tool/meter tick.
                if event in {"approval", "clarify"}:
                    drained = drain_blocking_prompts(api, session_id)
                    if drained["approvals"] or drained["clarifies"]:
                        row["auto_approve"]["drains"].append({"event": event, **drained})
                if event in {"apperror", "error"}:
                    event_data = payload if isinstance(payload, dict) else {}
                    error_type = str(event_data.get("type") or event).strip()
                    error_code = str(event_data.get("error_code") or "unknown").strip()
                    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", error_type):
                        error_type = event
                    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", error_code):
                        error_code = "unknown"
                    row["observations"].append({
                        "code": "MODEL_STREAM_ERROR",
                        "error_code": error_code,
                        "type": error_type,
                    })
                if trigger and not cancelled and _trigger_matched(trigger, event, payload if isinstance(payload, dict) else {}):
                    row["cancel"], row["cancel_response_ms"] = _dispatch_cancel(api, row["stream_id"])
                    cancelled = True
                    if not _cancel_accepted(row["cancel"]):
                        row["observations"].append({"code": "CANCEL_NOT_ACCEPTED", "detail": row["cancel"]})
                        if cancel_only:
                            row["alignment_failures"].append({"code": "CANCEL_NOT_ACCEPTED", "detail": row["cancel"]})
                    # Stop reading SSE after cancel; wait on /api/session/status instead.
                    break
                if event in {"done", "cancel", "apperror", "error", "stream_end"}:
                    break
        except (OSError, TimeoutError, urllib.error.URLError) as exc:
            row["observations"].append({"code": "STREAM_ERROR", "detail": type(exc).__name__})
        if trigger and not cancelled:
            row["observations"].append({"code": "CANCEL_TRIGGER_NOT_REACHED", "trigger": trigger})
            row["cancel"], row["cancel_response_ms"] = _dispatch_cancel(api, row["stream_id"])
            cancelled = True
            if not _cancel_accepted(row["cancel"]):
                row["observations"].append({"code": "CANCEL_NOT_ACCEPTED", "detail": row["cancel"]})
                if cancel_only:
                    row["alignment_failures"].append({"code": "CANCEL_NOT_ACCEPTED", "detail": row["cancel"]})
            elif cancel_only:
                # Fallback cancel still exercised the mechanism; keep as observation only.
                pass
    # After cancel (or natural stream end): drain prompts, then poll status readiness.
    row["cancelled"] = cancelled
    drained = drain_blocking_prompts(api, session_id)
    if drained["approvals"] or drained["clarifies"]:
        row["auto_approve"]["drains"].append({"event": "post_stream", **drained})
    readiness_after_stream, settled = _settle(api, session_id)
    row["readiness_after_stream"] = readiness_after_stream
    row["ready_for_next_start"] = readiness_after_stream.get("can_start_chat") is True
    if not settled:
        row["observations"].append({"code": "SESSION_NOT_READY_AFTER_STREAM"})
        row["alignment_failures"].append({"code": "SESSION_NOT_READY_AFTER_STREAM"})
        return row
    session = settled.get("session", {})
    row["turn_key"] = _turn_key(session, nonce)
    if cancel_only:
        # Cancel-verify batch only scores cancel/readiness, not artifact alignment.
        row["artifact_hashes"] = {}
        row["observations"].append({"code": "ARTIFACT_ALIGNMENT_SKIPPED", "reason": "cancel_verify"})
        if trigger and not cancelled:
            row["alignment_failures"].append({"code": "CANCEL_NOT_PERFORMED"})
        return row
    if cancelled and _cancel_accepted(row.get("cancel")):
        row["artifact_hashes"] = {}
        row["observations"].append({"code": "ARTIFACT_ALIGNMENT_SKIPPED", "reason": "cancelled"})
        return row
    manifest = api.request("GET", "/api/session/manifest?session_id=" + urllib.parse.quote(session_id)).get("manifest", {})
    failures, observations, hashes = evaluate_alignment(session, manifest, row, workspace)
    row["alignment_failures"].extend(failures)
    row["observations"].extend(observations)
    row["artifact_hashes"] = hashes
    return row


def run_campaign(
    session_count: int,
    turns: int,
    base_url: str,
    seed: int | None = None,
    context_mode: ContextMode = "first",
    prompt_source: PromptSource = "database",
    *,
    cancel_verify_session: bool = False,
    allow_concurrent: bool = False,
) -> int:
    if prompt_source == "model" and turns < 3:
        raise RuntimeError("prompt_source='model' requires turns >= 3 for the multi-turn file scenario")
    api = Api(base_url); health = api.request("GET", "/health")
    if health.get("status") != "ok":
        raise RuntimeError("WebUI must be healthy")
    if not allow_concurrent and (health.get("active_streams") or health.get("active_runs")):
        raise RuntimeError("WebUI must be healthy and have no active streams or runs")
    try:
        from api.config import get_config

        config = get_config()
        model_config = config.get("model") if isinstance(config, dict) else {}
        model = ((model_config or {}).get("default")) if isinstance(model_config, dict) else model_config
    except Exception as exc:
        raise RuntimeError("cannot read config.yaml default model") from exc
    if not model: raise RuntimeError("config.yaml model.default is required")

    state_dir = _state_dir()
    if prompt_source == "database":
        print(f"scanning history prompts from {state_dir}/sessions + session_manifest.db …", flush=True)
        full_pool = load_history_prompt_pool(state_dir)
        pool = pool_for_context_mode(full_pool, context_mode)
        if not pool:
            raise RuntimeError(
                "no stable file-generation prompts found for context_mode="
                f"{context_mode!r} under {state_dir}/sessions + session_manifest.db "
                f"(full_pool={len(full_pool)}, need turn-linked write_file/patch artifacts, "
                f"tools>={MIN_TOOLS}, exclude cron/campaign noise)"
            )
        first_n = sum(1 for item in pool if not item.needs_prefix_replay)
        replay_n = len(pool) - first_n
    elif prompt_source == "model":
        if context_mode != "first":
            raise RuntimeError("prompt_source='model' supports only context_mode='first'")
        print(f"generating {session_count} multi-turn file scenarios with {model!r} …", flush=True)
        pool = generate_model_prompt_pool(
            model,
            session_count,
            model_config=model_config,
        )
        if not pool:
            raise RuntimeError("model prompt generator returned no file-generation prompts")
        first_n, replay_n = len(pool), 0
    else:
        raise ValueError(f"unsupported prompt_source: {prompt_source!r}")
    rng = random.Random(seed)
    batch_specs = resolve_batch_specs(
        session_count, turns, cancel_verify_session=cancel_verify_session, rng=rng,
    )
    print(
        f"prompt pool ready: {len(pool)} prompts "
        f"(source={prompt_source}, first_turn={first_n}, replay={replay_n}, "
        f"mode={context_mode}, seed={seed!r})",
        flush=True,
    )
    if any(spec["kind"] == "cancel_verify" for spec in batch_specs):
        print(
            f"cancel-verify enabled: first session cancel-only with random triggers "
            f"(total batches={len(batch_specs)})",
            flush=True,
        )
    campaign_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    root = state_dir / "e2e_campaigns" / campaign_id; root.mkdir(parents=True, exist_ok=False)
    summary = {
        "campaign_id": campaign_id,
        "model": model,
        "base_url": base_url,
        "seed": seed,
        "context_mode": context_mode,
        "prompt_source": prompt_source,
        "history_pool_size": len(pool),
        "history_pool_first_turn": first_n,
        "history_pool_replay": replay_n,
        "history_source": str(state_dir),
        "cancellation_plan": cancellation_plan(turns),
        "cancel_verify_session": any(spec["kind"] == "cancel_verify" for spec in batch_specs),
        "batches": [],
    }
    summary_path = root / f"{campaign_id}-campaign.json"
    _persist_campaign_summary(summary_path, summary, completed=False)
    used: set[str] = set()
    total_batches = len(batch_specs)
    for spec in batch_specs:
        batch_index = int(spec["batch_index"])
        batch_kind = str(spec["kind"])
        cancel_plan = dict(spec["cancel_plan"])
        batch_root = root / "artifacts" / f"batch-{batch_index:02d}"
        if batch_kind == "cancel_verify":
            batch_root = root / "artifacts" / f"batch-{batch_index:02d}-cancel-verify"
        plain_workspace: Path | None = None
        plain_session_id = ""
        plain_session_ready = False
        report: dict[str, Any] = {
            "batch_index": batch_index,
            "batch_kind": batch_kind,
            "cancel_plan": cancel_plan,
            "rounds": [],
            "session_ids": [],
        }
        batch_question = pick_history_prompt(pool, rng, used) if prompt_source == "model" else None
        if batch_question is not None:
            report["scenario"] = asdict(batch_question)
        for turn in range(1, turns + 1):
            question = batch_question or pick_history_prompt(pool, rng, used)
            if question.needs_prefix_replay:
                workspace = batch_root / f"replay-turn-{turn:02d}"
                seed_workspace(workspace)
                session_id, replay_meta, workspace = create_replay_session(
                    api, state_dir, workspace, question, model,
                )
                if workspace is not None and replay_meta.get("fallback"):
                    seed_workspace(workspace)
                strategy = "replay_prefix"
            else:
                if not plain_session_id:
                    plain_session_id, plain_workspace = create_plain_session(api)
                    if plain_workspace is not None:
                        seed_workspace(plain_workspace)
                    plain_session_ready = bool(plain_session_id and plain_workspace)
                session_id = plain_session_id
                workspace = plain_workspace
                replay_meta = {"strategy": "first_turn_continuous"}
                strategy = "first_turn"
            if session_id and session_id not in report["session_ids"]:
                report["session_ids"].append(session_id)
            trigger = cancel_plan.get(turn)
            print(
                f"batch {batch_index}/{total_batches} kind={batch_kind} "
                f"trial {turn}/{turns}: strategy={strategy} "
                f"cancel={trigger or '-'} hist_sid={question.source_session_id} "
                f"hist_turn={question.turn} tools={question.n_tools} title={question.title!r}",
                flush=True,
            )
            if not session_id or workspace is None:
                report["rounds"].append({
                    "turn": turn,
                    "observations": [{"code": "SESSION_CREATE_FAILED"}],
                    "alignment_failures": [],
                    "history_prompt": asdict(question),
                    "context_strategy": strategy,
                    "replay_meta": replay_meta,
                    "cancel_trigger": trigger,
                })
                continue
            round_row = _run_round(
                api,
                session_id,
                workspace,
                question,
                campaign_id,
                turn,
                trigger,
                start_ready=(
                    batch_kind == "cancel_verify"
                    and session_id == plain_session_id
                    and plain_session_ready
                ),
                cancel_only=(batch_kind == "cancel_verify"),
                model_campaign=(prompt_source == "model"),
            )
            if batch_kind == "cancel_verify" and session_id == plain_session_id:
                plain_session_ready = round_row.get("ready_for_next_start") is True
            round_row["session_id"] = session_id
            round_row["context_strategy"] = strategy
            round_row["replay_meta"] = replay_meta
            report["rounds"].append(round_row)
        issues = [issue for row in report["rounds"] for issue in row.get("alignment_failures") or []]
        report["alignment_status"] = "FAIL" if issues else ("PASS" if report["rounds"] else "INDETERMINATE")
        primary = report["session_ids"][0] if report["session_ids"] else "uncreated"
        kind_suffix = "-cancel-verify" if batch_kind == "cancel_verify" else ""
        name = f"{campaign_id}-batch-{batch_index:02d}{kind_suffix}-{primary}.json"
        (root / name).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        summary["batches"].append({
            "batch_index": batch_index,
            "batch_kind": batch_kind,
            "session_ids": report["session_ids"],
            "report": name,
            "alignment_status": report["alignment_status"],
            "alignment_failure_count": len(issues),
            "sampled_prompts": [
                {
                    "turn": row.get("turn"),
                    "context_strategy": row.get("context_strategy"),
                    "session_id": row.get("session_id"),
                    "cancel_trigger": row.get("cancel_trigger"),
                    "source_session_id": (row.get("history_prompt") or {}).get("source_session_id"),
                    "source_turn": (row.get("history_prompt") or {}).get("turn"),
                    "title": (row.get("history_prompt") or {}).get("title"),
                    "n_tools": (row.get("history_prompt") or {}).get("n_tools"),
                    "replay_meta": row.get("replay_meta"),
                }
                for row in report["rounds"]
            ],
        })
        _persist_campaign_summary(summary_path, summary, completed=False)
    _persist_campaign_summary(summary_path, summary, completed=True)
    print(root)
    return 1 if any(batch["alignment_status"] == "FAIL" for batch in summary["batches"]) else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sessions", type=int, default=5, help="Number of campaign batches")
    parser.add_argument("--turns", type=int, default=15, help="Trials per batch")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--seed", type=int, default=None, help="RNG seed for reproducible history prompt sampling")
    parser.add_argument(
        "--prompt-source",
        choices=("database", "model"),
        default="model",
        help="database: sample history prompts; model: generate multi-turn file scenarios with the configured model",
    )
    parser.add_argument(
        "--context-mode",
        choices=("mixed", "first", "replay"),
        default="first",
        help="default first=only opening prompts; replay/mixed keep mid-turn import (experimental)",
    )
    parser.add_argument(
        "--cancel-verify",
        type=parse_bool_arg,
        default=True,
        metavar="BOOL",
        help="true: first session is cancel-only (every turn cancels with a random trigger); default true",
    )
    parser.add_argument(
        "--allow-concurrent",
        action="store_true",
        help="Allow campaign sessions while other WebUI streams or runs are active",
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="Delete campaign sessions and their campaign-owned workspaces, then exit",
    )
    args = parser.parse_args(argv)
    if args.cleanup:
        result = cleanup_campaign_test_data(Api(args.base_url))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1
    if args.sessions < 1:
        parser.error("--sessions must be >= 1")
    if args.prompt_source == "model" and args.turns < 3:
        parser.error("--prompt-source model requires --turns >= 3")
    if args.prompt_source == "database" and args.turns < 5:
        parser.error("--prompt-source database requires --turns >= 5")
    return run_campaign(
        args.sessions,
        args.turns,
        args.base_url,
        seed=args.seed,
        context_mode=args.context_mode,
        prompt_source=args.prompt_source,
        cancel_verify_session=args.cancel_verify,
        allow_concurrent=args.allow_concurrent,
    )


if __name__ == "__main__":
    raise SystemExit(main())

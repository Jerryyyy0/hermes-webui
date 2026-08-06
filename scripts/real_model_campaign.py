#!/usr/bin/env python3
"""Manual real-model E2E campaign for multi-turn artifact alignment.

Each trial question is randomly sampled from historical WebUI sessions that
stably produced write-sourced delivery artifacts (manifest evidence).

Default ``--context-mode first``: only opening-turn prompts, one continuous
plain session per batch (no transcript import / mid-turn replay).

Optional modes remain available but are not the default:
- replay: mid-turn prompts with transcript prefix via /api/session/import
- mixed: first-turn + replay

This is intentionally outside tests/ and is never run by CI or pytest.
"""
from __future__ import annotations

import argparse
import hashlib
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
SETTLE_TIMEOUT = 25
MIN_TOOLS = 5
PREFIX_TOOL_CONTENT_LIMIT = 12000
ContextMode = Literal["mixed", "first", "replay"]
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
            tool_ids: list[str] = []
            write_count = 0
            while cursor < len(messages):
                row = messages[cursor]
                if isinstance(row, dict) and row.get("role") == "user":
                    break
                if isinstance(row, dict) and row.get("role") == "assistant":
                    for tid, name in _tool_events(row):
                        tool_ids.append(tid)
                        if _is_write_tool(name):
                            write_count += 1
                cursor += 1
            index = cursor
            if _noise_prompt(prompt, session_id) or write_count < 1 or len(set(tool_ids)) < MIN_TOOLS:
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
                n_tools=len(set(tool_ids)),
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


def create_plain_session(api: Api, workspace: Path) -> str:
    created = api.request("POST", "/api/session/new", {"workspace": str(workspace), "worktree": False})
    session_id = str(created.get("session", {}).get("session_id") or "")
    if session_id:
        enable_auto_approve(api, session_id)
    return session_id


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
        })
    return out


def cleanup_campaign_test_data(api: "Api | None" = None, state_dir: Path | None = None) -> dict[str, Any]:
    """Delete campaign test sessions and wipe ``e2e_campaigns`` artifact trees.

    ``/api/session/delete`` does not remove workspaces; campaign artifacts live
    under ``HERMES_WEBUI_STATE_DIR/e2e_campaigns/`` and must be removed explicitly.
    """
    root = (state_dir or _state_dir()).expanduser().resolve()
    campaigns_root = root / "e2e_campaigns"
    sessions = list_campaign_test_sessions(root)
    deleted: list[str] = []
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

    return {
        "ok": not failed,
        "deleted_sessions": deleted,
        "failed_sessions": failed,
        "removed_campaign_dirs": removed_campaign_dirs,
        "campaigns_root": str(campaigns_root),
    }


def create_replay_session(
    api: Api,
    state_dir: Path,
    workspace: Path,
    question: HistoryPrompt,
    model: str,
) -> tuple[str, dict[str, Any]]:
    meta: dict[str, Any] = {"strategy": "replay_prefix", "prefix_messages": 0, "copied_files": []}
    prefix = load_prefix_messages(state_dir, question)
    meta["prefix_messages"] = len(prefix)
    if not prefix:
        meta["fallback"] = "missing_prefix_use_plain_session"
        return create_plain_session(api, workspace), meta
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
        return create_plain_session(api, workspace), meta
    enable_auto_approve(api, session_id)
    return session_id, meta


def seed_workspace(workspace: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "constraints.md").write_text(
        "请在工作区创建或修改真实文件完成交付；不要只在回复中描述结果；不要删除用户未要求删除的文件。\n",
        encoding="utf-8",
    )
    (workspace / "deliverables").mkdir(parents=True, exist_ok=True)


def build_history_prompt(question: HistoryPrompt, turn: int, nonce: str) -> tuple[str, str]:
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

    def events(self, stream_id: str):
        request = urllib.request.Request(self.base_url + "/api/chat/stream?stream_id=" + urllib.parse.quote(stream_id))
        with urllib.request.urlopen(request, timeout=ROUND_TIMEOUT) as response:
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
        path = (workspace / relative).resolve()
        if workspace.resolve() not in path.parents or not path.is_file() or path.is_symlink():
            failures.append({"code": "ARTIFACT_PATH_INVALID", "path": relative}); continue
        # Only the primary campaign delivery must embed NONCE; companion html/png
        # under deliverables/ are allowed without the marker.
        if _is_primary_delivery(relative) and ledger["nonce"] not in path.read_text(encoding="utf-8", errors="replace"):
            failures.append({"code": "ARTIFACT_NONCE_MISMATCH", "path": relative})
        hashes[relative] = _sha256(path)
        # Ownership conflicts matter for campaign outputs, not seed/prose noise
        # like constraints.md attributed across multiple turns.
        if relative.startswith("deliverables/") or relative.startswith("work/"):
            for other in manifest.get("turns", []):
                other_paths = {str(a.get("path") or "") for a in other.get("artifacts", [])}
                if other.get("turn_key") != actual_key and relative in other_paths:
                    failures.append({"code": "ARTIFACT_MULTI_TURN_OWNER", "path": relative, "other_turn": other.get("turn_key")})
    if not artifacts: observations.append({"code": "MODEL_NO_ARTIFACT"})
    return failures, observations, hashes


def _settle(api: Api, session_id: str) -> dict:
    until, latest = time.monotonic() + SETTLE_TIMEOUT, {}
    while time.monotonic() < until:
        latest = api.request("GET", "/api/session?session_id=" + urllib.parse.quote(session_id) + "&messages=1")
        if not latest.get("session", {}).get("active_stream_id"): return latest
        time.sleep(0.4)
    return latest


def _run_round(
    api: Api,
    session_id: str,
    workspace: Path,
    question: HistoryPrompt,
    campaign_id: str,
    turn: int,
    trigger: str | None,
) -> dict:
    nonce = uuid.uuid4().hex
    prompt, expected = build_history_prompt(question, turn, nonce)
    prompt = prompt.format(campaign_id=campaign_id, session_id=session_id)
    row: dict[str, Any] = {
        "turn": turn,
        "nonce": nonce,
        "prompt": prompt,
        "history_prompt": asdict(question),
        "expected_artifact_path": expected,
        "cancel_trigger": trigger,
        "events": [],
        "observations": [],
        "alignment_failures": [],
        "auto_approve": {"yolo": None, "drains": []},
    }
    row["auto_approve"]["yolo"] = enable_auto_approve(api, session_id)
    start = api.request("POST", "/api/chat/start", {"session_id": session_id, "workspace": str(workspace), "message": prompt})
    chat_start_received_at = time.monotonic()
    row["chat_start"], row["stream_id"] = start, str(start.get("stream_id") or "")
    if not row["stream_id"]:
        row["observations"].append({"code": "CHAT_START_FAILED", "detail": start.get("error", "missing stream_id")}); return row
    cancelled = False
    if trigger == "immediate_after_start":
        row["cancel_dispatch_delay_ms"] = round((time.monotonic() - chat_start_received_at) * 1000, 1)
        t0 = time.monotonic(); row["cancel"] = api.request("GET", "/api/chat/cancel?stream_id=" + urllib.parse.quote(row["stream_id"])); row["cancel_response_ms"] = round((time.monotonic() - t0) * 1000, 1); cancelled = True
        if row["cancel_dispatch_delay_ms"] > 250: row["alignment_failures"].append({"code": "IMMEDIATE_CANCEL_SLOW", "actual_ms": row["cancel_dispatch_delay_ms"]})
    try:
        for event, payload in api.events(row["stream_id"]):
            row["events"].append({"event": event, "payload": payload})
            # Drain only when the stream signals a blocking prompt; avoid
            # polling /api/approval|clarify/pending on every tool/meter tick.
            if event in {"approval", "clarify"}:
                drained = drain_blocking_prompts(api, session_id)
                if drained["approvals"] or drained["clarifies"]:
                    row["auto_approve"]["drains"].append({"event": event, **drained})
            matched = (trigger == "after_first_tool" and event in {"tool", "tool_complete"}) or (trigger == "after_manifest_delta" and event == "manifest_delta") or (trigger == "after_first_artifact" and event == "manifest_delta" and bool(payload.get("artifacts")))
            if matched and not cancelled: row["cancel"] = api.request("GET", "/api/chat/cancel?stream_id=" + urllib.parse.quote(row["stream_id"])); cancelled = True
            if event in {"done", "cancel", "apperror", "error", "stream_end"}: break
    except (OSError, TimeoutError, urllib.error.URLError) as exc:
        row["observations"].append({"code": "STREAM_ERROR", "detail": type(exc).__name__})
    if trigger and not cancelled:
        row["observations"].append({"code": "CANCEL_TRIGGER_NOT_REACHED", "trigger": trigger}); row["cancel"] = api.request("GET", "/api/chat/cancel?stream_id=" + urllib.parse.quote(row["stream_id"]))
    # Final drain in case the stream ended while still awaiting a prompt.
    drained = drain_blocking_prompts(api, session_id)
    if drained["approvals"] or drained["clarifies"]:
        row["auto_approve"]["drains"].append({"event": "post_stream", **drained})
    session = _settle(api, session_id).get("session", {})
    row["turn_key"] = _turn_key(session, nonce)
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
) -> int:
    api = Api(base_url); health = api.request("GET", "/health")
    if health.get("status") != "ok" or health.get("active_streams") or health.get("active_runs"):
        raise RuntimeError("WebUI must be healthy and have no active streams or runs")
    try:
        from api.config import get_config
        model = ((get_config().get("model") or {}).get("default"))
    except Exception as exc:
        raise RuntimeError("cannot read config.yaml default model") from exc
    if not model: raise RuntimeError("config.yaml model.default is required")

    state_dir = _state_dir()
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
    print(
        f"history pool ready: {len(pool)} prompts "
        f"(first_turn={first_n}, replay={replay_n}, mode={context_mode}, seed={seed!r})",
        flush=True,
    )
    rng = random.Random(seed)
    campaign_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    root = state_dir / "e2e_campaigns" / campaign_id; root.mkdir(parents=True, exist_ok=False)
    summary = {
        "campaign_id": campaign_id,
        "model": model,
        "base_url": base_url,
        "seed": seed,
        "context_mode": context_mode,
        "history_pool_size": len(pool),
        "history_pool_first_turn": first_n,
        "history_pool_replay": replay_n,
        "history_source": str(state_dir),
        "cancellation_plan": cancellation_plan(turns),
        "batches": [],
    }
    used: set[str] = set()
    for index in range(session_count):
        batch_root = root / "artifacts" / f"batch-{index + 1:02d}"
        plain_workspace = batch_root / "plain"
        seed_workspace(plain_workspace)
        plain_session_id = ""
        report: dict[str, Any] = {"batch_index": index + 1, "rounds": [], "session_ids": []}
        for turn in range(1, turns + 1):
            question = pick_history_prompt(pool, rng, used)
            if question.needs_prefix_replay:
                workspace = batch_root / f"replay-turn-{turn:02d}"
                seed_workspace(workspace)
                session_id, replay_meta = create_replay_session(api, state_dir, workspace, question, model)
                strategy = "replay_prefix"
            else:
                workspace = plain_workspace
                if not plain_session_id:
                    plain_session_id = create_plain_session(api, workspace)
                session_id = plain_session_id
                replay_meta = {"strategy": "first_turn_continuous"}
                strategy = "first_turn"
            if session_id and session_id not in report["session_ids"]:
                report["session_ids"].append(session_id)
            print(
                f"batch {index + 1}/{session_count} trial {turn}/{turns}: "
                f"strategy={strategy} hist_sid={question.source_session_id} "
                f"hist_turn={question.turn} tools={question.n_tools} title={question.title!r}",
                flush=True,
            )
            if not session_id:
                report["rounds"].append({
                    "turn": turn,
                    "observations": [{"code": "SESSION_CREATE_FAILED"}],
                    "alignment_failures": [],
                    "history_prompt": asdict(question),
                    "context_strategy": strategy,
                    "replay_meta": replay_meta,
                })
                continue
            round_row = _run_round(
                api, session_id, workspace, question, campaign_id, turn, cancellation_plan(turns).get(turn),
            )
            round_row["session_id"] = session_id
            round_row["context_strategy"] = strategy
            round_row["replay_meta"] = replay_meta
            report["rounds"].append(round_row)
        issues = [issue for row in report["rounds"] for issue in row.get("alignment_failures") or []]
        report["alignment_status"] = "FAIL" if issues else ("PASS" if report["rounds"] else "INDETERMINATE")
        primary = report["session_ids"][0] if report["session_ids"] else "uncreated"
        name = f"{campaign_id}-batch-{index + 1:02d}-{primary}.json"
        (root / name).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        summary["batches"].append({
            "batch_index": index + 1,
            "session_ids": report["session_ids"],
            "report": name,
            "alignment_status": report["alignment_status"],
            "alignment_failure_count": len(issues),
            "sampled_prompts": [
                {
                    "turn": row.get("turn"),
                    "context_strategy": row.get("context_strategy"),
                    "session_id": row.get("session_id"),
                    "source_session_id": (row.get("history_prompt") or {}).get("source_session_id"),
                    "source_turn": (row.get("history_prompt") or {}).get("turn"),
                    "title": (row.get("history_prompt") or {}).get("title"),
                    "n_tools": (row.get("history_prompt") or {}).get("n_tools"),
                    "replay_meta": row.get("replay_meta"),
                }
                for row in report["rounds"]
            ],
        })
    (root / f"{campaign_id}-campaign.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(root); return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sessions", type=int, default=1, help="Number of campaign batches")
    parser.add_argument("--turns", type=int, default=15, help="Trials per batch")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--seed", type=int, default=None, help="RNG seed for reproducible history prompt sampling")
    parser.add_argument(
        "--context-mode",
        choices=("mixed", "first", "replay"),
        default="first",
        help="default first=only opening prompts; replay/mixed keep mid-turn import (experimental)",
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="Delete campaign test sessions and wipe e2e_campaigns artifact trees, then exit",
    )
    args = parser.parse_args(argv)
    if args.cleanup:
        result = cleanup_campaign_test_data(Api(args.base_url))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1
    if args.sessions < 1 or args.turns < 5: parser.error("--sessions must be >= 1 and --turns must be >= 5")
    return run_campaign(
        args.sessions, args.turns, args.base_url, seed=args.seed, context_mode=args.context_mode,
    )


if __name__ == "__main__":
    raise SystemExit(main())

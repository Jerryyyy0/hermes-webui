"""Session manifest: structured todos, artifacts, and references from tool activity."""

from __future__ import annotations

import json
import copy
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ARTIFACT_IGNORE_RE = re.compile(
    r'(^|/)(?:\.git|\.hg|\.svn|node_modules|\.venv|venv|__pycache__|dist|build|\.next|\.cache)(?:/|$)'
)

ARTIFACT_MUTATION_TOOLS = frozenset({
    'write_file',
    'create_file',
    'edit_file',
    'patch',
    'apply_patch',
    'mcp_filesystem_write_file',
    'mcp_filesystem_edit_file',
})

REFERENCE_READ_TOOLS = frozenset({
    'read_file',
    'open_file',
    'view_file',
    'list_dir',
    'mcp_filesystem_read_file',
    'mcp_filesystem_list_directory',
})

REFERENCE_DISCOVERY_TOOLS = frozenset({
    'glob',
    'rg',
    'grep',
    'search',
    'semantic_search',
    'mcp_filesystem_search_files',
})

REFERENCE_DIR_TOOLS = frozenset({
    'list_dir',
    'mcp_filesystem_list_directory',
})

REFERENCE_SKILL_TOOLS = frozenset({
    'skill_view',
})

# Hermes Agent skill_manager_tool: single tool skill_manage(action=...).
SKILL_MANAGE_TOOL = 'skill_manage'

SKILL_MANAGE_MUTATION_ACTIONS = frozenset({
    'create',
    'edit',
    'patch',
    'write_file',
})

MANIFEST_PREVIEW_FILE = 'file'
MANIFEST_PREVIEW_SKILL = 'skill'

MEDIA_ARTIFACT_SOURCE = 'media'
_MEDIA_TOKEN_RE = re.compile(r'MEDIA:([^\s\)\]]+)')

PATH_ARG_KEYS = (
    'path',
    'file_path',
    'target',
    'destination',
    'filename',
    'directory',
    'target_directory',
)

TODO_STATUSES = frozenset({'pending', 'in_progress', 'completed', 'cancelled'})

_DIFF_PATH_RE = re.compile(
    r'(?:^|\n)(?:\+\+\+|---)\s+(?:[ab]/)([^\n\t]+)',
    re.MULTILINE,
)
_DIFF_ADD_UPDATE_RE = re.compile(
    r'^\*\*\* (?:Add|Update) File:\s+(.+)$',
    re.MULTILINE,
)

# Workspace-relative paths in tool output (conservative: must look like a path).
_RESULT_PATH_RE = re.compile(
    r'(?:^|[\s\'"`])([A-Za-z0-9_./-]+(?:\.[A-Za-z0-9]+))(?:[\s\'"`.,:;]|$)',
)


@dataclass
class ToolEvent:
    name: str
    args: dict[str, Any] = field(default_factory=dict)
    result: str = ''
    assistant_msg_idx: int | None = None
    tool_msg_idx: int | None = None
    tid: str = ''
    status: str = 'completed'
    source: str = 'message'


def _normalize_tool_name(name: str | None) -> str:
    return str(name or '').replace('functions.', '').strip().lower()


def _artifact_source_priority(source_tool: str) -> int:
    tool = str(source_tool or '').strip().lower()
    if tool in ARTIFACT_MUTATION_TOOLS:
        return 2
    if tool == MEDIA_ARTIFACT_SOURCE:
        return 1
    return 0


def _paths_from_assistant_media(text: str, workspace: Path) -> list[str]:
    if not text:
        return []
    paths: list[str] = []
    seen: set[str] = set()
    for ref in _MEDIA_TOKEN_RE.findall(text):
        if '://' in ref:
            continue
        normalized = _resolve_manifest_path(workspace, ref)
        if normalized and normalized not in seen:
            seen.add(normalized)
            paths.append(normalized)
    return paths


def _assistant_message_indices_for_turn(messages: list, turn_key: str) -> list[int]:
    key = str(turn_key or '').strip()
    if not key.startswith('turn:'):
        return []
    try:
        user_idx = int(key.split(':', 1)[1])
    except (TypeError, ValueError):
        return []
    if user_idx < 0 or user_idx >= len(messages or []):
        return []
    end_idx = len(messages) - 1
    for idx in range(user_idx + 1, len(messages)):
        message = messages[idx]
        if isinstance(message, dict) and message.get('role') == 'user':
            end_idx = idx - 1
            break
    return [
        idx for idx in range(user_idx, end_idx + 1)
        if isinstance(messages[idx], dict) and messages[idx].get('role') == 'assistant'
    ]


def _collect_media_artifact_events(messages: list, workspace: Path, *, turn_key: str = '') -> list[ToolEvent]:
    events: list[ToolEvent] = []
    seen: set[tuple[str, int]] = set()
    indices: list[int] = []
    if turn_key:
        indices = _assistant_message_indices_for_turn(messages, turn_key)
    else:
        indices = [
            idx for idx, message in enumerate(messages or [])
            if isinstance(message, dict) and message.get('role') == 'assistant'
        ]
    for msg_idx in indices:
        message = messages[msg_idx]
        text = _message_text(message.get('content'))
        for path in _paths_from_assistant_media(text, workspace):
            key = (path, msg_idx)
            if key in seen:
                continue
            seen.add(key)
            events.append(ToolEvent(
                name=MEDIA_ARTIFACT_SOURCE,
                args={'path': path},
                assistant_msg_idx=msg_idx,
                source='assistant_media',
            ))
    return events


def _parse_json_object(content: Any) -> dict | None:
    if isinstance(content, dict):
        return content
    if isinstance(content, str):
        text = content.strip()
        if not text:
            return None
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, TypeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _todo_items_from_payload(payload: dict | None) -> list[dict[str, str]]:
    if not payload or not isinstance(payload.get('todos'), list):
        return []
    items = []
    for item in payload['todos']:
        if not isinstance(item, dict):
            continue
        status = str(item.get('status') or '').strip().lower()
        if status and status not in TODO_STATUSES:
            status = 'unknown'
        items.append({
            'id': str(item.get('id') or ''),
            'content': str(item.get('content') or ''),
            'status': status,
        })
    return items


def _todo_items_from_text(text: Any) -> list[dict[str, str]]:
    return _todo_items_from_payload(_parse_json_object(text))


def _merge_todo_items(existing_items: list | None, incoming_items: list | None) -> list[dict[str, str]]:
    by_id: dict[str, dict[str, str]] = {}
    order: list[str] = []

    def add_item(item: Any, *, incoming: bool) -> None:
        if not isinstance(item, dict):
            return
        item_id = str(item.get('id') or '').strip()
        if not item_id:
            return
        current = by_id.get(item_id)
        if current is None:
            current = {'id': item_id, 'content': '', 'status': ''}
            by_id[item_id] = current
            order.append(item_id)
        content = str(item.get('content') or '').strip()
        status = str(item.get('status') or '').strip().lower()
        if status and status not in TODO_STATUSES:
            status = 'unknown'
        if content and content != '(no description)':
            current['content'] = content
        elif not incoming and content:
            current['content'] = content
        if status:
            current['status'] = status

    for existing in existing_items or []:
        add_item(existing, incoming=False)
    for incoming in incoming_items or []:
        add_item(incoming, incoming=True)
    return [
        {
            'id': row['id'],
            'content': row.get('content') or '',
            'status': row.get('status') or '',
        }
        for row in (by_id[item_id] for item_id in order)
    ]


def _todo_content_displayable(content: Any) -> bool:
    text = str(content or '').strip()
    return bool(text) and text != '(no description)'


def _public_todo_items(items: list | None) -> list[dict[str, str]]:
    public: list[dict[str, str]] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get('id') or '').strip()
        if not item_id or not _todo_content_displayable(item.get('content')):
            continue
        status = str(item.get('status') or '').strip().lower()
        if status and status not in TODO_STATUSES:
            status = 'unknown'
        public.append({
            'id': item_id,
            'content': str(item.get('content') or '').strip(),
            'status': status,
        })
    return public


def _apply_public_todos_to_manifest_delta(delta: dict[str, Any], live_manifest: dict[str, Any] | None) -> None:
    """Rewrite or drop delta.todos before client delivery (live state may keep id-only rows)."""
    if 'todos' not in delta:
        return
    live_items: list = []
    if isinstance(live_manifest, dict):
        live_todos = live_manifest.get('todos')
        if isinstance(live_todos, dict):
            live_items = list(live_todos.get('items') or [])
    public = _public_todo_items(live_items)
    if public:
        mode = 'replace_latest'
        incoming = delta.get('todos')
        if isinstance(incoming, dict) and incoming.get('mode'):
            mode = str(incoming.get('mode'))
        delta['todos'] = {'items': public, 'mode': mode}
    else:
        delta.pop('todos', None)


def _last_user_msg_idx(messages: list) -> int | None:
    last_idx = None
    for idx, message in enumerate(messages or []):
        if isinstance(message, dict) and message.get('role') == 'user':
            last_idx = idx
    return last_idx


def _parse_tool_args(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                text = part.get('text') or part.get('content')
                if text:
                    parts.append(str(text))
            elif part:
                parts.append(str(part))
        return '\n'.join(parts)
    if content is None:
        return ''
    return str(content)


def _clean_manifest_path_raw(raw: str | None) -> str:
    if not raw:
        return ''
    path = str(raw).strip().strip('`"\'')
    path = path.strip('<>),;:!?')
    path = path.strip('(<[{')
    if not path or len(path) > 240 or '://' in path:
        return ''
    if ARTIFACT_IGNORE_RE.search(path):
        return ''
    if not re.search(r'[./\\]', path) and not re.fullmatch(r'[\w][\w.-]*', path):
        return ''
    path = path.replace('\\', '/')
    if path.startswith('./'):
        path = path[2:]
    return path


def _normalize_manifest_path(raw: str | None) -> str:
    """Lightweight path cleanup without workspace context (tests, diff/result heuristics)."""
    path = _clean_manifest_path_raw(raw)
    if not path:
        return ''
    if path.startswith('/'):
        return path
    while path.startswith('../'):
        path = path[3:]
    return path


def _resolve_manifest_path(workspace: Path, raw: str | None) -> str:
    """Return a display path for a tool-referenced file.

    Workspace files are normalized to session-workspace-relative paths so the
    existing file browser can open them. Files outside the workspace keep their
    absolute path so they can still be listed as session artifacts/references.
    """
    path = _clean_manifest_path_raw(raw)
    if not path:
        return ''
    ws = workspace.expanduser().resolve()
    try:
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = (ws / path).resolve()
        else:
            candidate = candidate.resolve()
        rel = candidate.relative_to(ws)
        rel_str = rel.as_posix()
    except (ValueError, OSError):
        external = candidate.as_posix()
        return '' if ARTIFACT_IGNORE_RE.search(external) else external
    if rel_str in ('', '.'):
        return ''
    if ARTIFACT_IGNORE_RE.search(rel_str):
        return ''
    return rel_str


def _paths_from_args(args: dict[str, Any], workspace: Path) -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()

    def add(raw: Any) -> None:
        normalized = _resolve_manifest_path(workspace, raw if isinstance(raw, str) else '')
        if normalized and normalized not in seen:
            seen.add(normalized)
            paths.append(normalized)

    if not isinstance(args, dict):
        return paths
    for key in PATH_ARG_KEYS:
        value = args.get(key)
        if isinstance(value, str):
            add(value)
    for key in ('paths', 'target_directories'):
        value = args.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    add(item)
    edits = args.get('edits')
    if isinstance(edits, list):
        for edit in edits:
            if isinstance(edit, dict):
                add(edit.get('path'))
    return paths


def _paths_from_diff_text(text: str, workspace: Path) -> list[str]:
    if not text:
        return []
    paths: list[str] = []
    seen: set[str] = set()
    for pattern in (_DIFF_PATH_RE, _DIFF_ADD_UPDATE_RE):
        for match in pattern.finditer(text):
            normalized = _resolve_manifest_path(workspace, match.group(1).strip())
            if normalized and normalized not in seen:
                seen.add(normalized)
                paths.append(normalized)
    return paths


def _paths_from_result_text(text: str, *, files_only: bool, workspace: Path) -> list[str]:
    if not text:
        return []
    paths: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        for match in _RESULT_PATH_RE.finditer(line):
            candidate = match.group(1)
            if '/' not in candidate and '\\' not in candidate:
                continue
            normalized = _resolve_manifest_path(workspace, candidate)
            if not normalized or normalized in seen:
                continue
            if files_only and normalized.endswith('/'):
                continue
            seen.add(normalized)
            paths.append(normalized)
    return paths


def _collect_tool_events(messages: list, session_tool_calls: list | None) -> list[ToolEvent]:
    events: list[ToolEvent] = []
    pending: dict[str, dict[str, Any]] = {}

    for msg_idx, message in enumerate(messages or []):
        if not isinstance(message, dict):
            continue
        role = message.get('role')
        if role == 'assistant':
            content = message.get('content')
            if isinstance(content, list):
                for part in content:
                    if not isinstance(part, dict) or part.get('type') != 'tool_use':
                        continue
                    tid = str(part.get('id') or '')
                    name = _normalize_tool_name(part.get('name'))
                    args = part.get('input') if isinstance(part.get('input'), dict) else {}
                    if tid and name:
                        pending[tid] = {
                            'name': name,
                            'args': args,
                            'assistant_msg_idx': msg_idx,
                        }
            for tc in message.get('tool_calls') or []:
                if not isinstance(tc, dict):
                    continue
                tid = str(tc.get('id') or tc.get('call_id') or '')
                fn = tc.get('function') if isinstance(tc.get('function'), dict) else {}
                name = _normalize_tool_name(fn.get('name'))
                args = _parse_tool_args(fn.get('arguments'))
                if tid and name:
                    pending[tid] = {
                        'name': name,
                        'args': args,
                        'assistant_msg_idx': msg_idx,
                    }
            for tc in message.get('_partial_tool_calls') or []:
                if not isinstance(tc, dict):
                    continue
                name = _normalize_tool_name(tc.get('name'))
                if not name:
                    continue
                args = tc.get('args') if isinstance(tc.get('args'), dict) else {}
                events.append(ToolEvent(
                    name=name,
                    args=args,
                    result=str(tc.get('preview') or tc.get('snippet') or ''),
                    assistant_msg_idx=msg_idx,
                    tool_msg_idx=None,
                    tid=str(tc.get('tid') or tc.get('id') or ''),
                    status='in_progress' if not tc.get('done') else 'completed',
                    source='partial',
                ))
        elif role == 'tool':
            tid = str(message.get('tool_call_id') or message.get('tool_use_id') or '')
            result_text = _message_text(message.get('content'))
            meta = pending.pop(tid, None) if tid else None
            name = _normalize_tool_name(meta.get('name') if meta else message.get('name'))
            args = meta.get('args') if isinstance(meta, dict) else {}
            assistant_idx = meta.get('assistant_msg_idx') if isinstance(meta, dict) else None
            if name:
                events.append(ToolEvent(
                    name=name,
                    args=args if isinstance(args, dict) else {},
                    result=result_text,
                    assistant_msg_idx=assistant_idx,
                    tool_msg_idx=msg_idx,
                    tid=tid,
                    status='completed',
                    source='message',
                ))
            elif result_text:
                payload = _parse_json_object(message.get('content'))
                if payload and isinstance(payload.get('todos'), list):
                    events.append(ToolEvent(
                        name='todo',
                        args={},
                        result=result_text,
                        assistant_msg_idx=None,
                        tool_msg_idx=msg_idx,
                        tid=tid,
                        status='completed',
                        source='message',
                    ))

    for tc in session_tool_calls or []:
        if not isinstance(tc, dict):
            continue
        name = _normalize_tool_name(tc.get('name'))
        if not name or name == 'tool':
            continue
        args = tc.get('args') if isinstance(tc.get('args'), dict) else {}
        events.append(ToolEvent(
            name=name,
            args=args,
            result=str(tc.get('snippet') or ''),
            assistant_msg_idx=tc.get('assistant_msg_idx'),
            tool_msg_idx=None,
            tid=str(tc.get('tid') or ''),
            status='completed',
            source='session_tool_calls',
        ))
    return events


def _extract_latest_todos(messages: list) -> dict[str, Any]:
    latest_items: list[dict[str, Any]] = []
    source_tool_msg_idx = None
    source_timestamp = None
    user_idx = _last_user_msg_idx(messages)
    if user_idx is None:
        return {
            'items': [],
            'source_tool_msg_idx': None,
            'source_timestamp': None,
        }

    for msg_idx, message in enumerate(messages or []):
        if msg_idx <= user_idx:
            continue
        if not isinstance(message, dict) or message.get('role') != 'tool':
            continue
        items = _todo_items_from_text(message.get('content'))
        if not items:
            continue
        latest_items = _merge_todo_items(latest_items, items)
        source_tool_msg_idx = msg_idx
        source_timestamp = message.get('timestamp') or message.get('_ts')

    return {
        'items': latest_items,
        'source_tool_msg_idx': source_tool_msg_idx,
        'source_timestamp': source_timestamp,
    }


def _record_key(kind: str, path: str, source_tool: str, assistant_msg_idx: int | None) -> str:
    return f'{kind}|{path}|{source_tool}|{assistant_msg_idx if assistant_msg_idx is not None else -1}'


def _skill_record_key(skill_name: str, source_tool: str, assistant_msg_idx: int | None) -> str:
    return f'skill|{skill_name}|{source_tool}|{assistant_msg_idx if assistant_msg_idx is not None else -1}'


def _skill_name_from_args(args: dict[str, Any] | None) -> str:
    if not isinstance(args, dict):
        return ''
    for key in ('name', 'skill', 'skill_name'):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ''


def _skill_manage_action(args: dict[str, Any] | None) -> str:
    if not isinstance(args, dict):
        return ''
    return str(args.get('action') or '').strip().lower()


def _is_skill_manage_mutation_event(event: ToolEvent) -> bool:
    if _normalize_tool_name(event.name) != SKILL_MANAGE_TOOL:
        return False
    return _skill_manage_action(event.args if isinstance(event.args, dict) else None) in SKILL_MANAGE_MUTATION_ACTIONS


def _skill_path_from_manage_result(result: str) -> str:
    payload = _parse_json_object(result)
    if not isinstance(payload, dict) or not payload.get('success'):
        return ''
    return str(payload.get('path') or '').strip().strip('/')


def _skill_manifest_name_from_manage_event(event: ToolEvent) -> str:
    if str(event.status or '').strip().lower() == 'completed':
        from_result = _skill_path_from_manage_result(str(event.result or ''))
        if from_result:
            return from_result
    return _skill_name_from_args(event.args if isinstance(event.args, dict) else None)


def _skill_manifest_name_from_skills_path(raw_path: str, skills_dir: Path | None) -> str:
    if skills_dir is None:
        return ''
    path_text = str(raw_path or '').strip()
    if not path_text:
        return ''
    try:
        from integration.skills.utils import skill_path_within

        root = Path(skills_dir).expanduser().resolve()
        candidate = Path(path_text).expanduser().resolve()
        if not skill_path_within(root, candidate):
            return ''
        if candidate.name != 'SKILL.md':
            return ''
        rel = candidate.parent.relative_to(root)
        rel_str = rel.as_posix()
        if rel_str in ('', '.'):
            return ''
        return rel_str
    except (ImportError, OSError, ValueError):
        return ''


def _skill_artifact_needs_wire_gate(source_tool: str, row: dict) -> bool:
    if row.get('kind') != 'skill' and row.get('resource_type') != 'skill':
        return False
    tool = _normalize_tool_name(source_tool)
    if tool == SKILL_MANAGE_TOOL:
        return True
    return tool in ARTIFACT_MUTATION_TOOLS


def _skillhub_preview_available() -> bool:
    try:
        from integration.config import integration_enabled

        return bool(integration_enabled())
    except ImportError:
        return False


def _skills_dir_for_profile(profile: str | None) -> Path:
    from api.profiles import get_hermes_home_for_profile

    name = str(profile or '').strip() or 'default'
    return Path(get_hermes_home_for_profile(name)).expanduser().resolve() / 'skills'


def _skills_dir_for_session(session) -> Path:
    profile = getattr(session, 'profile', None) or None
    return _skills_dir_for_profile(profile)


def _skill_exists_in_dir(skills_dir: Path | None, name: str) -> bool:
    raw = str(name or '').strip().strip('/')
    if not raw or skills_dir is None:
        return False
    root = Path(skills_dir).expanduser().resolve()
    if not root.is_dir():
        return False
    try:
        from integration.skills.local_skills import _find_skill

        _skill_dir, skill_md = _find_skill(raw, root)
        return skill_md is not None and skill_md.is_file()
    except ImportError:
        return (root / raw / 'SKILL.md').is_file()


def _merge_file_records(
    records: dict[str, dict],
    *,
    kind: str,
    path: str,
    event: ToolEvent,
    entry_kind: str,
    workspace: Path,
) -> None:
    normalized = _resolve_manifest_path(workspace, path)
    if not normalized:
        return
    existing = records.get(normalized)
    payload = {
        'path': normalized,
        'kind': entry_kind,
        'source_tool': event.name,
        'assistant_msg_idx': event.assistant_msg_idx,
        'tool_msg_idx': event.tool_msg_idx,
        'tid': event.tid,
        'status': event.status,
        'source': event.source,
        'previewable': entry_kind == 'file',
    }
    if existing:
        hits = existing.setdefault('hits', [])
        hit_key = _record_key(kind, normalized, event.name, event.assistant_msg_idx)
        if not any(h.get('_key') == hit_key for h in hits):
            hits.append({**payload, '_key': hit_key})
        existing['hit_count'] = len(hits)
        if _artifact_source_priority(event.name) > _artifact_source_priority(existing.get('source_tool', '')):
            existing.update({
                'source_tool': event.name,
                'assistant_msg_idx': event.assistant_msg_idx,
                'tool_msg_idx': event.tool_msg_idx,
                'tid': event.tid,
                'status': event.status,
                'source': event.source,
            })
        return
    payload['hits'] = [{**payload, '_key': _record_key(kind, normalized, event.name, event.assistant_msg_idx)}]
    payload['hit_count'] = 1
    records[normalized] = payload


def _merge_skill_records(
    records: dict[str, dict],
    *,
    skill_name: str,
    event: ToolEvent,
) -> None:
    if not skill_name:
        return
    existing = records.get(skill_name)
    payload = {
        'path': skill_name,
        'kind': 'skill',
        'resource_type': 'skill',
        'skill_name': skill_name,
        'source_tool': event.name,
        'assistant_msg_idx': event.assistant_msg_idx,
        'tool_msg_idx': event.tool_msg_idx,
        'tid': event.tid,
        'status': event.status,
        'source': event.source,
    }
    if existing:
        hits = existing.setdefault('hits', [])
        hit_key = _skill_record_key(skill_name, event.name, event.assistant_msg_idx)
        if not any(h.get('_key') == hit_key for h in hits):
            hits.append({**payload, '_key': hit_key})
        existing['hit_count'] = len(hits)
        return
    payload['hits'] = [{**payload, '_key': _skill_record_key(skill_name, event.name, event.assistant_msg_idx)}]
    payload['hit_count'] = 1
    records[skill_name] = payload


def _clean_record_keys(rows: list[dict]) -> list[dict]:
    for row in rows:
        for hit in row.get('hits') or []:
            hit.pop('_key', None)
    return rows


def _message_turns(messages: list) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    for idx, message in enumerate(messages or []):
        if not isinstance(message, dict) or message.get('role') != 'user':
            continue
        if turns:
            turns[-1]['end_msg_idx'] = idx - 1
        turns.append({
            'turn_key': f'turn:{idx}',
            'user_msg_idx': idx,
            'start_msg_idx': idx,
            'end_msg_idx': len(messages or []) - 1,
            'artifacts': [],
            'references': [],
        })
    return turns


def _turn_key_for_event(event: ToolEvent, turns: list[dict[str, Any]]) -> str | None:
    idx = event.assistant_msg_idx
    if idx is None:
        idx = event.tool_msg_idx
    if isinstance(idx, bool) or not isinstance(idx, int):
        return None
    for turn in reversed(turns):
        start = turn.get('start_msg_idx')
        end = turn.get('end_msg_idx')
        if isinstance(start, int) and isinstance(end, int) and start <= idx <= end:
            return str(turn.get('turn_key') or '')
    return None


def _extract_manifest_records(
    events: list[ToolEvent],
    workspace: Path,
    messages: list | None = None,
    *,
    skills_dir: Path | None = None,
) -> tuple[list[dict], list[dict], list[dict]]:
    artifacts: dict[str, dict] = {}
    references: dict[str, dict] = {}
    turns = _message_turns(messages or [])
    turn_rows: dict[str, dict] = {turn['turn_key']: {**turn, 'artifacts': {}, 'references': {}} for turn in turns}

    for event in events:
        name = event.name
        args_paths = _paths_from_args(event.args, workspace)
        text_blobs = []
        if event.result:
            text_blobs.append(event.result)
        if event.args:
            try:
                text_blobs.append(json.dumps(event.args, ensure_ascii=False))
            except (TypeError, ValueError):
                pass
        diff_paths: list[str] = []
        for blob in text_blobs:
            diff_paths.extend(_paths_from_diff_text(blob, workspace))
        turn_key = _turn_key_for_event(event, turns)
        turn = turn_rows.get(turn_key or '')

        def add_artifact(path: str) -> None:
            _merge_file_records(
                artifacts, kind='artifact', path=path, event=event, entry_kind='file', workspace=workspace,
            )
            if turn is not None:
                _merge_file_records(
                    turn['artifacts'], kind='artifact', path=path, event=event, entry_kind='file', workspace=workspace,
                )

        def add_reference(path: str, entry_kind: str) -> None:
            _merge_file_records(
                references, kind='reference', path=path, event=event, entry_kind=entry_kind, workspace=workspace,
            )
            if turn is not None:
                _merge_file_records(
                    turn['references'], kind='reference', path=path, event=event, entry_kind=entry_kind, workspace=workspace,
                )

        def add_skill_artifact(path: str) -> None:
            skill_name = _skill_manifest_name_from_skills_path(path, skills_dir)
            if not skill_name:
                return
            _merge_skill_records(artifacts, skill_name=skill_name, event=event)
            if turn is not None:
                _merge_skill_records(turn['artifacts'], skill_name=skill_name, event=event)

        if name in ARTIFACT_MUTATION_TOOLS:
            artifact_paths = args_paths + diff_paths
            artifact_paths.extend(_paths_from_diff_text(event.result, workspace))
            for path in artifact_paths:
                if _skill_manifest_name_from_skills_path(path, skills_dir):
                    add_skill_artifact(path)
                else:
                    add_artifact(path)

        if name == MEDIA_ARTIFACT_SOURCE:
            for path in args_paths:
                add_artifact(path)

        if name in REFERENCE_READ_TOOLS:
            for path in args_paths:
                entry_kind = 'dir' if name in REFERENCE_DIR_TOOLS else 'file'
                add_reference(path, entry_kind)

        if name in REFERENCE_SKILL_TOOLS:
            skill_name = _skill_name_from_args(event.args)
            if skill_name:
                _merge_skill_records(references, skill_name=skill_name, event=event)
                if turn is not None:
                    _merge_skill_records(turn['references'], skill_name=skill_name, event=event)

        if _is_skill_manage_mutation_event(event):
            skill_name = _skill_manifest_name_from_manage_event(event)
            if skill_name:
                _merge_skill_records(artifacts, skill_name=skill_name, event=event)
                if turn is not None:
                    _merge_skill_records(turn['artifacts'], skill_name=skill_name, event=event)

        if name == 'todo' and turn is not None:
            items = _todo_items_from_text(event.result)
            if items:
                previous_items = []
                if isinstance(turn.get('todo_snapshot'), dict):
                    previous_items = turn['todo_snapshot'].get('items') or []
                turn['todo_snapshot'] = {
                    'items': _merge_todo_items(previous_items, items),
                    'source_tool_msg_idx': event.tool_msg_idx,
                    'source_timestamp': None,
                }

    artifact_list = sorted(artifacts.values(), key=lambda row: row['path'])
    reference_list = sorted(references.values(), key=lambda row: row['path'])
    _clean_record_keys(artifact_list + reference_list)
    turn_list: list[dict] = []
    for turn in turn_rows.values():
        turn_artifacts = sorted(turn.get('artifacts', {}).values(), key=lambda row: row['path'])
        turn_references = sorted(turn.get('references', {}).values(), key=lambda row: row['path'])
        _clean_record_keys(turn_artifacts + turn_references)
        turn_list.append({
            'turn_key': turn.get('turn_key'),
            'user_msg_idx': turn.get('user_msg_idx'),
            'start_msg_idx': turn.get('start_msg_idx'),
            'end_msg_idx': turn.get('end_msg_idx'),
            'artifacts': turn_artifacts,
            'references': turn_references,
            **({'todo_snapshot': turn['todo_snapshot']} if turn.get('todo_snapshot') else {}),
        })
    return artifact_list, reference_list, turn_list


def _extract_artifacts_and_references(
    events: list[ToolEvent],
    workspace: Path,
    *,
    skills_dir: Path | None = None,
) -> tuple[list[dict], list[dict]]:
    artifacts, references, _turns = _extract_manifest_records(events, workspace, skills_dir=skills_dir)
    return artifacts, references


def _workspace_relative_path(workspace: Path, rel: str) -> tuple[str, bool]:
    if not rel:
        return '', False
    try:
        from api.workspace import safe_resolve_ws
        target = safe_resolve_ws(workspace, rel)
        rel_to_ws = target.relative_to(workspace.expanduser().resolve()).as_posix()
        return rel_to_ws, True
    except (ValueError, OSError, FileNotFoundError):
        return rel, False


def _serialize_manifest_row(path: str, preview: str, source_tool: str) -> dict[str, str]:
    return {
        'path': path,
        'preview': preview,
        'source_tool': source_tool,
    }


def _session_media_preview_path(workspace: Path, rel: str, entry_kind: str) -> str | None:
    if entry_kind == 'dir':
        return None
    _ws_rel, in_workspace = _workspace_relative_path(workspace, rel)
    if in_workspace:
        return None
    path_text = str(rel or '').strip()
    if not path_text:
        return None
    try:
        from api.workspace import MAX_FILE_BYTES, is_workspace_cruft_basename

        target = Path(path_text).expanduser().resolve()
        if not target.is_file():
            return None
        if is_workspace_cruft_basename(target.name):
            return None
        if target.stat().st_size > MAX_FILE_BYTES:
            return None
        return target.as_posix()
    except (ValueError, OSError, FileNotFoundError):
        return None


def _file_preview_path(workspace: Path, rel: str, entry_kind: str) -> str | None:
    if entry_kind == 'dir':
        return None
    ws_rel, in_workspace = _workspace_relative_path(workspace, rel)
    if not in_workspace:
        return None
    try:
        from api.workspace import MAX_FILE_BYTES, is_workspace_cruft_basename, safe_resolve_ws

        target = safe_resolve_ws(workspace, ws_rel)
        if not target.is_file():
            return None
        if is_workspace_cruft_basename(target.name):
            return None
        if target.stat().st_size > MAX_FILE_BYTES:
            return None
        return ws_rel
    except (ValueError, OSError, FileNotFoundError):
        return None


def _row_to_wire(row: dict, workspace: Path, skills_dir: Path | None = None) -> dict[str, str] | None:
    source_tool = str(row.get('source_tool') or '').strip()
    if not source_tool:
        return None
    if row.get('kind') == 'skill' or row.get('resource_type') == 'skill':
        skill_name = str(row.get('skill_name') or row.get('path') or '').strip()
        if not skill_name or not _skillhub_preview_available():
            return None
        if _skill_artifact_needs_wire_gate(source_tool, row):
            if str(row.get('status') or '').strip().lower() == 'in_progress':
                return None
            if not _skill_exists_in_dir(skills_dir, skill_name):
                return None
        return _serialize_manifest_row(skill_name, MANIFEST_PREVIEW_SKILL, source_tool)
    rel = str(row.get('path') or '').strip()
    entry_kind = str(row.get('kind') or 'file')
    preview_path = _file_preview_path(workspace, rel, entry_kind)
    if preview_path:
        return _serialize_manifest_row(preview_path, MANIFEST_PREVIEW_FILE, source_tool)
    if source_tool == MEDIA_ARTIFACT_SOURCE:
        media_path = _session_media_preview_path(workspace, rel, entry_kind)
        if media_path:
            return _serialize_manifest_row(media_path, MANIFEST_PREVIEW_FILE, source_tool)
    return None


def _rows_to_wire(
    rows: list[dict] | None,
    workspace: Path,
    skills_dir: Path | None = None,
) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows or []:
        wire = _row_to_wire(row, workspace, skills_dir)
        if wire is None:
            continue
        path = wire['path']
        if path in seen:
            continue
        seen.add(path)
        out.append(wire)
    return sorted(out, key=lambda item: item['path'])


def _turn_to_wire(turn: dict, workspace: Path, skills_dir: Path | None = None) -> dict[str, Any]:
    return {
        'turn_key': str(turn.get('turn_key') or ''),
        'artifacts': _rows_to_wire(turn.get('artifacts'), workspace, skills_dir),
        'references': _rows_to_wire(turn.get('references'), workspace, skills_dir),
    }


def _wire_todos(todos: dict[str, Any] | None) -> dict[str, list]:
    items = todos.get('items') if isinstance(todos, dict) else None
    return {'items': _public_todo_items(items)}


def _turn_sort_key(turn_key: str | None) -> tuple[int, str]:
    key = str(turn_key or '')
    if key.startswith('turn:'):
        try:
            return (int(key.split(':', 1)[1]), key)
        except (TypeError, ValueError):
            pass
    return (1_000_000_000, key)


def _merge_rows_by_path(existing_rows: list | None, incoming_rows: list | None) -> list[dict]:
    rows: dict[str, dict] = {}
    for row in list(existing_rows or []) + list(incoming_rows or []):
        if not isinstance(row, dict):
            continue
        path = str(row.get('path') or '').strip()
        preview = str(row.get('preview') or '').strip()
        source_tool = str(row.get('source_tool') or '').strip()
        if not path or preview not in (MANIFEST_PREVIEW_FILE, MANIFEST_PREVIEW_SKILL) or not source_tool:
            continue
        rows[path] = _serialize_manifest_row(path, preview, source_tool)
    return sorted(rows.values(), key=lambda item: item['path'])


def _merge_turn_rows(existing_turns: list | None, incoming_turns: list | None) -> list[dict]:
    turns: dict[str, dict] = {}
    for turn in list(existing_turns or []) + list(incoming_turns or []):
        if not isinstance(turn, dict):
            continue
        key = str(turn.get('turn_key') or '').strip()
        if not key:
            continue
        current = turns.get(key) or {'turn_key': key, 'artifacts': [], 'references': []}
        turns[key] = {
            'turn_key': key,
            'artifacts': _merge_rows_by_path(current.get('artifacts'), turn.get('artifacts')),
            'references': _merge_rows_by_path(current.get('references'), turn.get('references')),
        }
    return sorted(turns.values(), key=lambda turn: _turn_sort_key(turn.get('turn_key')))


def merge_manifest_delta(base: dict[str, Any] | None, delta: dict[str, Any] | None, scope: str = 'live') -> dict[str, Any]:
    """Merge one live manifest delta into a manifest-shaped payload.

    The merge is intentionally idempotent by path/turn so SSE replay can safely
    re-apply already-seen deltas before the final persisted manifest arrives.
    """
    base_manifest: dict[str, Any] = copy.deepcopy(base or {})
    delta = copy.deepcopy(delta or {})
    if not delta:
        return base_manifest

    if isinstance(delta.get('todos'), dict) and delta['todos'].get('items') is not None:
        existing_todos = base_manifest.get('todos') if isinstance(base_manifest.get('todos'), dict) else {}
        base_manifest['todos'] = {
            'items': _merge_todo_items(existing_todos.get('items') or [], delta['todos'].get('items') or []),
        }
    else:
        base_manifest.setdefault('todos', {'items': []})

    base_manifest['artifacts'] = _merge_rows_by_path(base_manifest.get('artifacts'), delta.get('artifacts'))
    base_manifest['references'] = _merge_rows_by_path(base_manifest.get('references'), delta.get('references'))
    incoming_turns = delta.get('turns')
    if not incoming_turns and delta.get('turn_key'):
        incoming_turns = [{
            'turn_key': delta.get('turn_key'),
            'artifacts': delta.get('artifacts') or [],
            'references': delta.get('references') or [],
        }]
    base_manifest['turns'] = _merge_turn_rows(base_manifest.get('turns'), incoming_turns)
    if delta.get('stream_id'):
        base_manifest['live'] = {
            'stream_id': delta.get('stream_id'),
            'source': scope,
        }
    return base_manifest


def extract_manifest_delta_from_tool_event(
    event: ToolEvent,
    workspace: Path,
    *,
    session_id: str = '',
    stream_id: str = '',
    turn_key: str = '',
    sequence: int | None = None,
    source_kind: str = '',
    skills_dir: Path | None = None,
) -> dict[str, Any]:
    """Build a manifest_delta SSE payload from one explicit tool event."""
    normalized_event = ToolEvent(
        name=_normalize_tool_name(event.name),
        args=event.args if isinstance(event.args, dict) else {},
        result=str(event.result or ''),
        assistant_msg_idx=event.assistant_msg_idx,
        tool_msg_idx=event.tool_msg_idx,
        tid=str(event.tid or ''),
        status=str(event.status or 'completed'),
        source=event.source or 'stream',
    )
    artifacts, references = _extract_artifacts_and_references(
        [normalized_event], workspace, skills_dir=skills_dir,
    )
    payload: dict[str, Any] = {
        'version': 1,
        'session_id': str(session_id or ''),
        'stream_id': str(stream_id or ''),
        'turn_key': str(turn_key or ''),
        'source': {
            'kind': source_kind or normalized_event.source,
            'tool': normalized_event.name,
            'tid': normalized_event.tid,
            'status': normalized_event.status,
        },
        'artifacts': _rows_to_wire(artifacts, workspace, skills_dir),
        'references': _rows_to_wire(references, workspace, skills_dir),
    }
    if sequence is not None:
        payload['sequence'] = sequence
    if normalized_event.name == 'todo' and normalized_event.status != 'in_progress':
        items = _todo_items_from_text(normalized_event.result)
        if items:
            payload['todos'] = {
                'items': items,
                'mode': 'replace_latest',
            }
    return payload


def extract_manifest_delta_from_assistant_media(
    messages: list,
    workspace: Path,
    *,
    session_id: str = '',
    stream_id: str = '',
    turn_key: str = '',
    sequence: int | None = None,
) -> dict[str, Any]:
    """Build a manifest_delta SSE payload from assistant MEDIA: tokens in one turn."""
    turn_key = str(turn_key or '').strip()
    if not turn_key:
        return {}
    events = _collect_media_artifact_events(messages, workspace, turn_key=turn_key)
    if not events:
        return {}
    artifacts, _references, turns = _extract_manifest_records(events, workspace, messages)
    wire_artifacts = _rows_to_wire(artifacts, workspace)
    if not wire_artifacts:
        return {}
    current_turn = next((turn for turn in turns if turn.get('turn_key') == turn_key), None)
    turn_artifacts = _rows_to_wire(
        (current_turn or {}).get('artifacts') or artifacts,
        workspace,
    )
    payload: dict[str, Any] = {
        'version': 1,
        'session_id': str(session_id or ''),
        'stream_id': str(stream_id or ''),
        'turn_key': turn_key,
        'source': {
            'kind': 'turn_complete',
            'tool': MEDIA_ARTIFACT_SOURCE,
            'tid': '',
            'status': 'completed',
        },
        'artifacts': wire_artifacts,
        'turns': [{
            'turn_key': turn_key,
            'artifacts': turn_artifacts,
            'references': [],
        }],
    }
    if sequence is not None:
        payload['sequence'] = sequence
    return payload


def _load_display_messages(session) -> list:
    from api.models import (
        get_cli_session_messages,
        get_state_db_session_messages,
        merge_session_messages_append_only,
    )
    from api.routes import (
        _is_messaging_session_record,
        _lookup_cli_session_metadata,
        _merged_session_messages_for_display,
        _session_requires_cli_metadata_lookup,
    )

    sid = session.session_id
    cli_meta = _lookup_cli_session_metadata(sid) if _session_requires_cli_metadata_lookup(session) else {}
    is_messaging = _is_messaging_session_record(session) or _is_messaging_session_record(cli_meta)
    profile = getattr(session, 'profile', None) or None
    if is_messaging:
        cli_messages = get_cli_session_messages(sid)
        return _merged_session_messages_for_display(session, cli_messages)
    state_db_messages = get_state_db_session_messages(sid, profile=profile)
    return merge_session_messages_append_only(
        session.messages,
        state_db_messages,
        truncation_watermark=getattr(session, 'truncation_watermark', None),
    )


def build_session_manifest(session) -> dict[str, Any]:
    """Build structured todos, artifacts, and references for one session."""
    messages = _load_display_messages(session)
    tool_calls = list(getattr(session, 'tool_calls', None) or [])
    workspace = Path(str(session.workspace)).expanduser().resolve()
    skills_dir = _skills_dir_for_session(session)
    events = _collect_tool_events(messages, tool_calls)
    events.extend(_collect_media_artifact_events(messages, workspace))
    todos = _extract_latest_todos(messages)
    artifacts, references, turns = _extract_manifest_records(
        events, workspace, messages, skills_dir=skills_dir,
    )
    return {
        'todos': _wire_todos(todos),
        'artifacts': _rows_to_wire(artifacts, workspace, skills_dir),
        'references': _rows_to_wire(references, workspace, skills_dir),
        'turns': [_turn_to_wire(turn, workspace, skills_dir) for turn in turns],
    }

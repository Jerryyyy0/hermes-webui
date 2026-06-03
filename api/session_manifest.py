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

    for msg_idx, message in enumerate(messages or []):
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
        return
    payload['hits'] = [{**payload, '_key': _record_key(kind, normalized, event.name, event.assistant_msg_idx)}]
    payload['hit_count'] = 1
    records[normalized] = payload


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

        if name in ARTIFACT_MUTATION_TOOLS:
            for path in args_paths + diff_paths:
                add_artifact(path)
            for path in _paths_from_diff_text(event.result, workspace):
                add_artifact(path)

        if name in REFERENCE_READ_TOOLS:
            for path in args_paths:
                entry_kind = 'dir' if name in REFERENCE_DIR_TOOLS else 'file'
                add_reference(path, entry_kind)

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


def _extract_artifacts_and_references(events: list[ToolEvent], workspace: Path) -> tuple[list[dict], list[dict]]:
    artifacts, references, _turns = _extract_manifest_records(events, workspace)
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


def _external_file_preview(path: str, kind: str) -> dict[str, Any]:
    preview = {
        'exists': False,
        'size': None,
        'mtime': None,
        'kind': kind,
        'workspace_relative_path': path,
        'absolute_path': path,
        'in_workspace': False,
        'previewable': False,
    }
    try:
        target = Path(path).expanduser().resolve()
        stat = target.stat()
    except (OSError, FileNotFoundError, ValueError):
        return preview
    preview.update({
        'exists': True,
        'mtime': stat.st_mtime,
        'kind': 'dir' if target.is_dir() else 'file',
    })
    if target.is_file():
        preview['size'] = stat.st_size
    return preview


def _enrich_file_rows(rows: list[dict], workspace: Path) -> list[dict]:
    enriched = []
    for row in rows:
        rel = row.get('path') or ''
        preview = {
            'exists': False,
            'size': None,
            'mtime': None,
            'kind': row.get('kind') or 'file',
            'workspace_relative_path': rel,
            'in_workspace': False,
            'previewable': False,
        }
        ws_rel, in_workspace = _workspace_relative_path(workspace, rel)
        preview['workspace_relative_path'] = ws_rel
        preview['in_workspace'] = in_workspace
        if not in_workspace:
            row = {**row, 'preview': _external_file_preview(rel, row.get('kind') or 'file')}
            enriched.append(row)
            continue
        if row.get('kind') == 'dir':
            row = {**row, 'preview': preview}
            enriched.append(row)
            continue
        try:
            from api.workspace import safe_resolve_ws
            target = safe_resolve_ws(workspace, rel)
            if target.is_file():
                stat = target.stat()
                preview.update({
                    'exists': True,
                    'size': stat.st_size,
                    'mtime': stat.st_mtime,
                    'previewable': True,
                })
            elif target.is_dir():
                preview.update({'exists': True, 'kind': 'dir', 'previewable': False})
        except (ValueError, OSError, FileNotFoundError):
            pass
        row = {**row, 'preview': preview}
        enriched.append(row)
    return enriched


def _manifest_counts(manifest: dict[str, Any]) -> dict[str, int]:
    todos = manifest.get('todos') if isinstance(manifest.get('todos'), dict) else {}
    return {
        'todos': len(todos.get('items') or []),
        'artifacts': len(manifest.get('artifacts') or []),
        'references': len(manifest.get('references') or []),
        'turns': len(manifest.get('turns') or []),
    }


def _merge_rows_by_path(existing_rows: list | None, incoming_rows: list | None) -> list[dict]:
    rows: dict[str, dict] = {}
    for row in list(existing_rows or []) + list(incoming_rows or []):
        if not isinstance(row, dict):
            continue
        path = str(row.get('path') or '').strip()
        if not path:
            continue
        current = rows.get(path)
        if current is None:
            rows[path] = copy.deepcopy(row)
            continue
        merged = {**current, **copy.deepcopy(row)}
        hits = []
        seen = set()
        for hit in list(current.get('hits') or []) + list(row.get('hits') or []):
            if not isinstance(hit, dict):
                continue
            key = (
                hit.get('path') or path,
                hit.get('source_tool') or '',
                hit.get('tid') or '',
                hit.get('assistant_msg_idx'),
                hit.get('tool_msg_idx'),
            )
            if key in seen:
                continue
            seen.add(key)
            hits.append(copy.deepcopy(hit))
        if hits:
            merged['hits'] = hits
            merged['hit_count'] = len(hits)
        rows[path] = merged
    return sorted(rows.values(), key=lambda row: row.get('path') or '')


def _merge_turn_rows(existing_turns: list | None, incoming_turns: list | None) -> list[dict]:
    turns: dict[str, dict] = {}
    for turn in list(existing_turns or []) + list(incoming_turns or []):
        if not isinstance(turn, dict):
            continue
        key = str(turn.get('turn_key') or '').strip()
        if not key:
            continue
        current = turns.get(key)
        if current is None:
            current = {
                'turn_key': key,
                'user_msg_idx': turn.get('user_msg_idx'),
                'start_msg_idx': turn.get('start_msg_idx'),
                'end_msg_idx': turn.get('end_msg_idx'),
                'artifacts': [],
                'references': [],
            }
        merged = {**current, **{k: copy.deepcopy(v) for k, v in turn.items() if k not in ('artifacts', 'references')}}
        merged['artifacts'] = _merge_rows_by_path(current.get('artifacts'), turn.get('artifacts'))
        merged['references'] = _merge_rows_by_path(current.get('references'), turn.get('references'))
        turns[key] = merged

    def sort_key(turn: dict) -> tuple[int, str]:
        idx = turn.get('user_msg_idx')
        return (idx if isinstance(idx, int) else 1_000_000_000, str(turn.get('turn_key') or ''))

    return sorted(turns.values(), key=sort_key)


def merge_manifest_delta(base: dict[str, Any] | None, delta: dict[str, Any] | None, scope: str = 'live') -> dict[str, Any]:
    """Merge one live manifest delta into a manifest-shaped payload.

    The merge is intentionally idempotent by path/turn so SSE replay can safely
    re-apply already-seen deltas before the final persisted manifest arrives.
    """
    base_manifest: dict[str, Any] = copy.deepcopy(base or {})
    delta = copy.deepcopy(delta or {})
    if not delta:
        base_manifest['counts'] = _manifest_counts(base_manifest)
        return base_manifest

    for key in ('session_id', 'workspace'):
        if delta.get(key) and not base_manifest.get(key):
            base_manifest[key] = delta[key]
    if isinstance(delta.get('todos'), dict) and delta['todos'].get('items') is not None:
        existing_todos = base_manifest.get('todos') if isinstance(base_manifest.get('todos'), dict) else {}
        base_manifest['todos'] = {
            'items': _merge_todo_items(existing_todos.get('items') or [], delta['todos'].get('items') or []),
            'source_tool_msg_idx': delta['todos'].get('source_tool_msg_idx'),
            'source_timestamp': delta['todos'].get('source_timestamp'),
            'mode': delta['todos'].get('mode') or 'replace_latest',
        }
    else:
        base_manifest.setdefault('todos', {'items': [], 'source_tool_msg_idx': None, 'source_timestamp': None})

    base_manifest['artifacts'] = _merge_rows_by_path(base_manifest.get('artifacts'), delta.get('artifacts'))
    base_manifest['references'] = _merge_rows_by_path(base_manifest.get('references'), delta.get('references'))
    incoming_turns = delta.get('turns')
    if not incoming_turns and delta.get('turn_key'):
        incoming_turns = [{
            'turn_key': delta.get('turn_key'),
            'artifacts': delta.get('artifacts') or [],
            'references': delta.get('references') or [],
            **({'todo_snapshot': delta.get('todos')} if delta.get('todos') else {}),
        }]
    base_manifest['turns'] = _merge_turn_rows(base_manifest.get('turns'), incoming_turns)
    if delta.get('stream_id'):
        base_manifest['live'] = {
            'stream_id': delta.get('stream_id'),
            'source': scope,
        }
    base_manifest['counts'] = _manifest_counts(base_manifest)
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
    artifacts, references = _extract_artifacts_and_references([normalized_event], workspace)
    artifacts = _enrich_file_rows(artifacts, workspace)
    references = _enrich_file_rows(references, workspace)
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
        'artifacts': artifacts,
        'references': references,
    }
    if sequence is not None:
        payload['sequence'] = sequence
    if normalized_event.name == 'todo' and normalized_event.status != 'in_progress':
        items = _todo_items_from_text(normalized_event.result)
        if items:
            payload['todos'] = {
                'items': items,
                'mode': 'replace_latest',
                'source_tool_msg_idx': normalized_event.tool_msg_idx,
                'source_timestamp': None,
            }
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
    events = _collect_tool_events(messages, tool_calls)
    todos = _extract_latest_todos(messages)
    workspace = Path(str(session.workspace)).expanduser().resolve()
    artifacts, references, turns = _extract_manifest_records(events, workspace, messages)
    artifacts = _enrich_file_rows(artifacts, workspace)
    references = _enrich_file_rows(references, workspace)
    for turn in turns:
        turn['artifacts'] = _enrich_file_rows(turn.get('artifacts') or [], workspace)
        turn['references'] = _enrich_file_rows(turn.get('references') or [], workspace)
    return {
        'session_id': session.session_id,
        'workspace': str(workspace),
        'todos': todos,
        'artifacts': artifacts,
        'references': references,
        'turns': turns,
        'counts': {
            'todos': len(todos.get('items') or []),
            'artifacts': len(artifacts),
            'references': len(references),
            'turns': len(turns),
        },
    }

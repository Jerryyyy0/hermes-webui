"""Session manifest: structured todos, artifacts, and references from tool activity."""

from __future__ import annotations

import json
import copy
import logging
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from integration.agent_message_semantics.audit import log_control_message
from integration.agent_message_semantics.classifier import is_non_anchor_control_message

logger = logging.getLogger(__name__)

def _is_synthetic_control_message(message) -> bool:
    """Return whether Agent provenance says this row is not a user anchor."""
    return is_non_anchor_control_message(message)

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

# General execution tools are not mutation tools. Their artifacts require a
# successful completion plus an explicit, statically-resolvable output operand.
EXECUTION_ARTIFACT_TOOLS = frozenset({'terminal'})
_MAX_TERMINAL_COMMAND_LENGTH = 16 * 1024
_MAX_TERMINAL_SEGMENTS = 32
_MAX_EXECUTION_ARTIFACTS = 16

ARTIFACT_EXCLUSION_READ_TOOLS = frozenset({
    'read_file',
    'open_file',
    'view_file',
    'mcp_filesystem_read_file',
})

REFERENCE_DISCOVERY_TOOLS = frozenset({
    'glob',
    'rg',
    'grep',
    'search',
    'semantic_search',
    'mcp_filesystem_search_files',
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
MANIFEST_STATUS_EXPIRED = 'expired'

MEDIA_ARTIFACT_SOURCE = 'media'
ASSISTANT_PROSE_ARTIFACT_SOURCE = 'assistant_prose'
TURN_RECONCILE_SOURCE = 'reconcile'
_MEDIA_TOKEN_RE = re.compile(r'MEDIA:([^\s\]]+)')
_BROAD_FILENAME_EXT_RE = re.compile(
    r'([\w\u00b7\u4e00-\u9fff/._\-\(\)（）]{1,240}\.[A-Za-z0-9]{2,8})'
)
_LAST_ASSISTANT_TILDE_PATH_RE = re.compile(
    r'(~/[^\s`\'"<>|，,；;。：)\]]{1,240}\.[A-Za-z0-9]{2,8})'
)
_REFERENCE_ONLY_TOOLS = (
    ARTIFACT_EXCLUSION_READ_TOOLS
    | REFERENCE_DISCOVERY_TOOLS
    | REFERENCE_SKILL_TOOLS
)

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
    if tool in (MEDIA_ARTIFACT_SOURCE, ASSISTANT_PROSE_ARTIFACT_SOURCE):
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
        raw_ref = ref
        if raw_ref.endswith(')'):
            try:
                raw_path = Path(raw_ref).expanduser()
                stripped_path = Path(raw_ref[:-1]).expanduser()
                if not raw_path.exists() and stripped_path.exists():
                    raw_ref = raw_ref[:-1]
            except (OSError, ValueError):
                pass
        normalized = _resolve_manifest_path(workspace, raw_ref)
        if normalized and normalized not in seen:
            seen.add(normalized)
            paths.append(normalized)
    return paths


def _assistant_path_candidates(text: str) -> list[str]:
    """Return explicit file-like mentions from the final assistant message."""
    if not text or not isinstance(text, str):
        return []
    candidates: list[str] = []
    seen: set[str] = set()
    for match in _BROAD_FILENAME_EXT_RE.finditer(text):
        raw = str(match.group(1) or '').strip()
        if raw and raw not in seen:
            seen.add(raw)
            candidates.append(raw)
    for match in _LAST_ASSISTANT_TILDE_PATH_RE.finditer(text):
        raw = str(match.group(1) or '').strip()
        if raw and raw not in seen:
            seen.add(raw)
            candidates.append(raw)
    return candidates


def _unique_basename_match(raw: str, paths: list[str] | tuple[str, ...] | set[str]) -> str:
    """Return the one canonical path whose basename exactly matches *raw*."""
    basename = Path(str(raw or '')).name
    if not basename:
        return ''
    matches = {str(path).strip() for path in paths if Path(str(path or '')).name == basename and str(path or '').strip()}
    return next(iter(matches)) if len(matches) == 1 else ''


def _paths_from_last_assistant_message(
    text: str,
    workspace: Path,
    *,
    strong_paths: list[str] | tuple[str, ...] | set[str] = (),
    prior_artifact_paths: list[str] | tuple[str, ...] | set[str] = (),
) -> list[str]:
    """Extract final-message deliveries without guessing a directory from prose."""
    paths: list[str] = []
    seen: set[str] = set()

    def add(raw: str) -> None:
        if not raw or '://' in raw or len(paths) >= _MAX_EXECUTION_ARTIFACTS:
            return
        raw = str(raw).strip()
        is_bare = '/' not in raw and not raw.startswith('~')
        normalized = ''
        if is_bare:
            # A basename can be resolved only from unambiguous structured evidence.
            normalized = _unique_basename_match(raw, strong_paths)
            if not normalized:
                normalized = _unique_basename_match(raw, prior_artifact_paths)
            if not normalized:
                normalized = _resolve_manifest_path(workspace, raw)
        else:
            normalized = _resolve_manifest_path(workspace, raw)
        if not normalized or normalized in seen or normalized.startswith('uploads/'):
            return
        if not _artifact_path_is_real(workspace, normalized):
            return
        seen.add(normalized)
        paths.append(normalized)

    for raw in _assistant_path_candidates(text):
        add(raw)
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
        if _is_synthetic_control_message(message):
            log_control_message("manifest_media_skip", message)
            continue
        text = _message_text(message.get('content'))
        # Skip context-compaction messages — those are system-generated
        # handoffs, not media produced during the turn.
        if text.startswith('[CONTEXT COMPACTION \u2014 REFERENCE ONLY]'):
            continue
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
def _collect_final_assistant_artifact_events(
    messages: list,
    workspace: Path,
    *,
    turn_key: str = '',
    strong_paths: list[str] | tuple[str, ...] | set[str] = (),
    prior_artifact_paths: list[str] | tuple[str, ...] | set[str] = (),
) -> list[ToolEvent]:
    indices = (
        _assistant_message_indices_for_turn(messages, turn_key)
        if turn_key
        else [
            idx for idx, message in enumerate(messages or [])
            if isinstance(message, dict) and message.get('role') == 'assistant'
        ]
    )
    if not indices:
        return []
    msg_idx = indices[-1]
    text = _message_text(messages[msg_idx].get('content'))
    if text.startswith('[CONTEXT COMPACTION \u2014 REFERENCE ONLY]'):
        return []
    return [
        ToolEvent(
            name=ASSISTANT_PROSE_ARTIFACT_SOURCE,
            args={'path': path},
            assistant_msg_idx=msg_idx,
            source='assistant_prose',
        )
        for path in _paths_from_last_assistant_message(
            text,
            workspace,
            strong_paths=strong_paths,
            prior_artifact_paths=prior_artifact_paths,
        )
    ]


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


def _parse_leading_json_object(content: Any) -> dict | None:
    """Parse a leading JSON object, allowing trailing non-JSON text.

    Used for tool results such as ``{"success": false, ...}\\n[Tool loop warning: ...]``.
    """
    direct = _parse_json_object(content)
    if direct is not None:
        return direct
    if not isinstance(content, str):
        return None
    text = content.strip()
    if not text or text[0] != '{':
        return None
    decoder = json.JSONDecoder()
    try:
        parsed, _end = decoder.raw_decode(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


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


def _canonical_manifest_file_key(path: str, workspace: Path | None = None) -> str:
    """Return an existence-independent key for artifact dedupe and read evidence."""
    text = str(path or '').strip().strip('`"\'')
    if not text:
        return ''
    if text.lower().startswith('file://'):
        text = text[7:]
    text = text.replace('\\', '/')
    while text.startswith('./'):
        text = text[2:]
    is_absolute = text.startswith('/')
    parts: list[str] = []
    for part in text.split('/'):
        if part in ('', '.'):
            continue
        if part == '..':
            if parts:
                parts.pop()
            continue
        parts.append(part)
    normalized = '/'.join(parts)
    if is_absolute:
        normalized = f'/{normalized}' if normalized else '/'
    if not normalized or normalized == '/':
        return ''
    if is_absolute:
        try:
            # Resolve parents for symlink-stable keys without requiring the leaf file.
            normalized = Path(normalized).expanduser().resolve().as_posix()
        except (OSError, ValueError):
            pass
    if workspace is not None:
        try:
            ws_key = Path(workspace).expanduser().resolve().as_posix().rstrip('/')
            if is_absolute and (normalized == ws_key or normalized.startswith(f'{ws_key}/')):
                return normalized[len(ws_key):].lstrip('/')
            if is_absolute:
                return normalized
        except (OSError, ValueError):
            if is_absolute:
                return normalized
    return normalized.lstrip('/')


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
        raw_candidate = Path(path).expanduser()
        if not raw_candidate.is_absolute():
            candidate = (ws / raw_candidate).resolve()
            parts = raw_candidate.parts
            if (
                len(parts) > 1
                and parts[0] == ws.name
                and not candidate.exists()
            ):
                alias_candidate = (ws / Path(*parts[1:])).resolve()
                if alias_candidate.exists():
                    candidate = alias_candidate
        else:
            candidate = raw_candidate.resolve()
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


def artifact_workspace_root_for_session(session) -> Path:
    """Return the one root used by every ordinary-file artifact stage."""
    from api.workspace import artifact_workspace_root_for_session as _artifact_root

    return _artifact_root(session)


def _rebase_file_artifact_records(
    rows: list[dict] | None,
    source_workspace: Path,
    artifact_workspace: Path,
) -> list[dict]:
    """Rebase source-workspace records onto the shared artifact root.

    Tool arguments use the session workspace as their relative-path base, while
    preview and store identity use the enclosing artifact root. Keep the two
    operations separate so `write_file("report.md")` still resolves inside a
    managed session, but an explicit absolute path at the default workspace
    root can also be surfaced as an artifact.
    """
    source = source_workspace.expanduser().resolve()
    root = artifact_workspace.expanduser().resolve()
    out: list[dict] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        copied = dict(row)
        if _is_skill_manifest_row(copied) or "workspace_root" in copied:
            out.append(copied)
            continue
        raw = str(copied.get("path") or "").strip()
        if not raw:
            out.append(copied)
            continue
        try:
            candidate = Path(raw).expanduser()
            candidate = candidate.resolve() if candidate.is_absolute() else (source / candidate).resolve()
            copied["path"] = candidate.relative_to(root).as_posix()
        except (OSError, RuntimeError, ValueError):
            if str(copied.get("source_tool") or "") != MEDIA_ARTIFACT_SOURCE:
                continue
        out.append(copied)
    return out


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


def _tool_event_succeeded(event: ToolEvent) -> bool:
    """Accept completed tool events unless their structured result reports failure."""
    if str(event.status or '').strip().lower() != 'completed':
        return False
    payload = _parse_json_object(event.result)
    if not payload:
        return True
    if payload.get('success') is False:
        return False
    status = str(payload.get('status') or '').strip().lower()
    if status in {'error', 'failed', 'failure', 'cancelled', 'canceled'}:
        return False
    exit_code = payload.get('exit_code')
    if isinstance(exit_code, int) and not isinstance(exit_code, bool):
        return exit_code == 0
    return True


def _execution_event_succeeded(event: ToolEvent) -> bool:
    return _tool_event_succeeded(event)


def _terminal_output_paths(command: str, workspace: Path) -> list[str]:
    """Return explicit output operands for a conservative shell subset."""
    if not isinstance(command, str) or not command or len(command) > _MAX_TERMINAL_COMMAND_LENGTH:
        return []
    if any(marker in command for marker in ('$', '`', '*', '?', '$(', '<(', '>(', '\\n')):
        return []
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=';&|')
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return []
    if len(tokens) > _MAX_TERMINAL_SEGMENTS * 16:
        return []

    workspace = workspace.expanduser().resolve()
    cwd = workspace
    paths: list[str] = []
    seen: set[str] = set()
    segment: list[str] = []

    def add_output(raw: str, command_cwd: Path) -> None:
        if not raw or raw.startswith('-') or '://' in raw:
            return
        try:
            candidate = Path(raw).expanduser()
            candidate = candidate.resolve() if candidate.is_absolute() else (command_cwd / candidate).resolve()
            rel = candidate.relative_to(workspace).as_posix()
        except (OSError, ValueError):
            return
        if not rel or ARTIFACT_IGNORE_RE.search(rel) or rel in seen or not candidate.is_file():
            return
        seen.add(rel)
        paths.append(rel)

    def process_segment(values: list[str]) -> None:
        nonlocal cwd
        if not values:
            return
        if values[0] == 'cd' and len(values) == 2:
            try:
                next_cwd = Path(values[1]).expanduser()
                cwd = next_cwd.resolve() if next_cwd.is_absolute() else (cwd / next_cwd).resolve()
                cwd.relative_to(workspace)
            except (OSError, ValueError):
                cwd = workspace
            return
        if values[0] == 'cp':
            operands = values[1:]
            if operands[:1] == ['--']:
                operands = operands[1:]
            if len(operands) == 2 and not any(value.startswith('-') for value in operands):
                add_output(operands[1], cwd)
            return
        for index, token in enumerate(values):
            raw = ''
            if token in ('-o', '--output') and index + 1 < len(values):
                raw = values[index + 1]
            elif token.startswith('--output=') or token.startswith('--print-to-pdf='):
                raw = token.split('=', 1)[1]
            if raw:
                add_output(raw, cwd)
                if len(paths) >= _MAX_EXECUTION_ARTIFACTS:
                    return

        # md2word's public CLI shape is: python md2word.py INPUT OUTPUT [options].
        # It is intentionally the only positional-output grammar accepted here.
        script_index = next(
            (index for index, token in enumerate(values) if Path(token).name == 'md2word.py'),
            None,
        )
        if script_index is None or script_index == 0:
            return
        runner = Path(values[script_index - 1]).name.lower()
        if not runner.startswith('python'):
            return
        positional = [token for token in values[script_index + 1:] if not token.startswith('-')]
        if len(positional) >= 2:
            add_output(positional[1], cwd)

    for token in tokens + [';']:
        if token in (';', '&&', '|', '||'):
            process_segment(segment)
            if len(paths) >= _MAX_EXECUTION_ARTIFACTS:
                break
            segment = []
        else:
            segment.append(token)
    return paths


def _execution_artifact_paths(event: ToolEvent, workspace: Path) -> list[str]:
    if event.name not in EXECUTION_ARTIFACT_TOOLS or not _execution_event_succeeded(event):
        return []
    command = event.args.get('command') if isinstance(event.args, dict) else None
    return _terminal_output_paths(command, workspace)


def _collect_turn_artifact_entries_from_events(
    events: list[ToolEvent],
    workspace: Path,
    skills_dir: Path | None = None,
) -> list[dict[str, str]]:
    """Extract file and skill artifact entries from scoped tool/prose events."""
    entries: list[dict[str, str]] = []
    seen: set[str] = set()

    def add_file(path: str, source_tool: str) -> None:
        if not path or path in seen:
            return
        seen.add(path)
        entries.append({
            'path': path,
            'source_tool': source_tool,
            'preview': MANIFEST_PREVIEW_FILE,
        })

    def add_skill(raw_name: str, source_tool: str) -> None:
        canonical = _canonical_skill_manifest_path(raw_name, skills_dir)
        if not canonical or canonical in seen:
            return
        seen.add(canonical)
        entries.append({
            'path': canonical,
            'source_tool': source_tool,
            'preview': MANIFEST_PREVIEW_SKILL,
        })

    for ev in events:
        if ev.name in ARTIFACT_MUTATION_TOOLS:
            if not _tool_event_succeeded(ev):
                continue
            candidate_paths: list[str] = []
            candidate_paths.extend(_paths_from_args(ev.args, workspace))
            candidate_paths.extend(_paths_from_diff_text(ev.result or '', workspace))
            for raw_path in candidate_paths:
                skill_name = _skill_manifest_name_from_skills_path(raw_path, skills_dir)
                if skill_name:
                    add_skill(skill_name, ev.name)
                else:
                    add_file(raw_path, ev.name)
            continue
        if _is_skill_manage_mutation_event(ev):
            if not _tool_event_succeeded(ev):
                continue
            skill_name = _skill_manifest_name_from_manage_event(ev, skills_dir)
            if skill_name:
                add_skill(skill_name, SKILL_MANAGE_TOOL)
            continue
        if ev.name in EXECUTION_ARTIFACT_TOOLS:
            for raw_path in _execution_artifact_paths(ev, workspace):
                add_file(raw_path, ev.name)
            continue
        if ev.name in (MEDIA_ARTIFACT_SOURCE, ASSISTANT_PROSE_ARTIFACT_SOURCE):
            for raw_path in _paths_from_args(ev.args, workspace):
                add_file(raw_path, ev.name)
    return entries


def _extract_turn_artifact_paths(
    turn_messages: list,
    tool_calls: list | None,
    workspace: Path,
    *,
    start_msg_idx: int | None = None,
    end_msg_idx: int | None = None,
    skills_dir: Path | None = None,
) -> list[str]:
    """Extract workspace-relative artifact paths from tool events in a turn's message slice.

    Only paths produced by ARTIFACT_MUTATION_TOOLS (write_file, edit_file, patch, etc.)
    are included. Paths from assistant prose mentions are NOT included — those are
    handled by the reconcile pass and are not reliable for cross-turn attribution.

    ``start_msg_idx`` / ``end_msg_idx`` are indices in the **full** session messages
    array. When provided, ``session.tool_calls`` snippets are scoped with
    ``_tool_calls_for_turn`` so earlier turns' write_file paths are not attributed
    to the current turn.
    """
    entries = _extract_turn_artifact_entries(
        turn_messages,
        tool_calls,
        workspace,
        start_msg_idx=start_msg_idx,
        end_msg_idx=end_msg_idx,
        skills_dir=skills_dir,
    )
    return [
        entry['path']
        for entry in entries
        if str(entry.get('preview') or MANIFEST_PREVIEW_FILE) != MANIFEST_PREVIEW_SKILL
    ]


def _extract_turn_artifact_entries(
    turn_messages: list,
    tool_calls: list | None,
    workspace: Path,
    *,
    start_msg_idx: int | None = None,
    end_msg_idx: int | None = None,
    skills_dir: Path | None = None,
    prior_artifact_paths: list[str] | tuple[str, ...] | set[str] = (),
) -> list[dict[str, str]]:
    """Extract artifact entries (path, source_tool, preview) from tool events in a turn.

    Same scoping contract as ``_extract_turn_artifact_paths`` but returns dict
    entries so the persisted ``turn_artifacts`` retains the original tool name
    (patch, edit_file, write_file, skill_manage, …) instead of losing it behind a
    hardcoded ``'write_file'``.
    """
    scoped_tool_calls = _tool_calls_for_turn(
        tool_calls,
        start_msg_idx=start_msg_idx,
        end_msg_idx=end_msg_idx,
    )
    events = _collect_tool_events(turn_messages, scoped_tool_calls)
    events.extend(_collect_media_artifact_events(turn_messages, workspace))
    strong_entries = _collect_turn_artifact_entries_from_events(events, workspace, skills_dir=skills_dir)
    strong_paths = [
        str(entry.get('path') or '').strip()
        for entry in strong_entries
        if isinstance(entry, dict) and str(entry.get('preview') or MANIFEST_PREVIEW_FILE) == MANIFEST_PREVIEW_FILE
    ]
    events.extend(_collect_final_assistant_artifact_events(
        turn_messages,
        workspace,
        strong_paths=strong_paths,
        prior_artifact_paths=prior_artifact_paths,
    ))
    return _collect_turn_artifact_entries_from_events(events, workspace, skills_dir=skills_dir)


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
                raw_status = str(tc.get('status') or '').strip().lower()
                failed = bool(tc.get('is_error')) or raw_status in {
                    'error', 'failed', 'failure', 'cancelled', 'canceled',
                }
                events.append(ToolEvent(
                    name=name,
                    args=args,
                    result=str(tc.get('preview') or tc.get('snippet') or ''),
                    assistant_msg_idx=msg_idx,
                    tool_msg_idx=None,
                    tid=str(tc.get('tid') or tc.get('id') or ''),
                    status='failed' if failed else ('completed' if tc.get('done') is True else 'in_progress'),
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
        if tc.get('done') is not True or tc.get('is_error'):
            continue
        raw_status = str(tc.get('status') or '').strip().lower()
        if raw_status in {'error', 'failed', 'failure', 'cancelled', 'canceled'}:
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


def _skill_path_from_manage_result(result: str, skills_dir: Path | None = None) -> str:
    payload = _parse_json_object(result)
    if not isinstance(payload, dict) or not payload.get('success'):
        return ''
    raw_path = str(payload.get('path') or '').strip()
    if not raw_path:
        return ''
    canonical = _skill_manifest_name_from_skill_dir_path(raw_path, skills_dir)
    return canonical or raw_path.strip('/')


def _skill_manifest_name_from_manage_event(event: ToolEvent, skills_dir: Path | None = None) -> str:
    if str(event.status or '').strip().lower() == 'completed':
        from_result = _skill_path_from_manage_result(str(event.result or ''), skills_dir)
        if from_result:
            return from_result
    return _skill_name_from_args(event.args if isinstance(event.args, dict) else None)


def _skill_manifest_name_from_view_event(event: ToolEvent, skills_dir: Path | None = None) -> str:
    """Return the canonical skill manifest path for a successful skill_view event.

    Only explicit ``success is True`` results produce a reference. Failed,
    ambiguous, empty, or unparseable results never fall back to args alone.
    """
    if str(event.status or '').strip().lower() != 'completed':
        return ''
    payload = _parse_leading_json_object(str(event.result or ''))
    if not isinstance(payload, dict) or payload.get('success') is not True:
        return ''
    for key in ('path', 'skill_path', 'manifest_path'):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            canonical = _skill_manifest_name_from_skill_dir_path(value, skills_dir)
            if canonical:
                return canonical
            return _canonical_skill_manifest_path(value, skills_dir)
    result_name = _skill_name_from_args(payload)
    if result_name:
        return _canonical_skill_manifest_path(result_name, skills_dir)
    # Args are only a last resort when the successful result omitted path/name.
    args_name = _skill_name_from_args(event.args if isinstance(event.args, dict) else None)
    if args_name:
        return _canonical_skill_manifest_path(args_name, skills_dir)
    return ''


def _skill_manifest_name_from_skill_dir_path(raw_path: str, skills_dir: Path | None) -> str:
    """Return skills_dir-relative name for a skill directory or its SKILL.md."""
    if skills_dir is None:
        return ''
    path_text = str(raw_path or '').strip()
    if not path_text:
        return ''
    try:
        from integration.skills.utils import skill_path_within

        root = Path(skills_dir).expanduser().resolve()
        raw_candidate = Path(path_text).expanduser()
        candidates = [raw_candidate]
        if not raw_candidate.is_absolute():
            candidates.append(root / path_text)
            if path_text.startswith(('Users/', 'private/', 'tmp/', 'var/', 'home/')):
                candidates.append(Path('/' + path_text).expanduser())
        for candidate in candidates:
            resolved = candidate.resolve()
            skill_dir = resolved.parent if resolved.name == 'SKILL.md' else resolved
            if not skill_path_within(root, skill_dir):
                continue
            if not (skill_dir / 'SKILL.md').is_file():
                continue
            rel = skill_dir.relative_to(root)
            rel_str = rel.as_posix()
            if rel_str not in ('', '.'):
                return rel_str
    except (ImportError, OSError, ValueError):
        return ''
    return ''


def _skill_path_string_normalize(raw: str) -> str:
    """Normalize skill path forms to a skills_dir-relative name without FS checks."""
    text = str(raw or '').strip().replace('\\', '/').strip('/')
    if not text:
        return ''
    if text.endswith('/SKILL.md'):
        text = text[: -len('/SKILL.md')]
    elif text.endswith('SKILL.md') and '/' in text:
        text = text.rsplit('/', 1)[0]
    search = text if text.startswith('/') else f'/{text}'
    marker = '/skills/'
    idx = search.lower().rfind(marker)
    if idx >= 0:
        text = search[idx + len(marker):]
    return text.strip('/')


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


def _is_skill_manifest_row(row: dict) -> bool:
    if row.get('kind') == 'skill' or row.get('resource_type') == 'skill':
        return True
    return str(row.get('preview') or '').strip() == MANIFEST_PREVIEW_SKILL


def _is_file_manifest_row(row: dict) -> bool:
    if not isinstance(row, dict) or _is_skill_manifest_row(row):
        return False
    preview = str(row.get('preview') or '').strip()
    if preview == MANIFEST_PREVIEW_FILE:
        return True
    if preview and preview != MANIFEST_PREVIEW_FILE:
        return False
    kind = str(row.get('kind') or row.get('entry_kind') or '').strip()
    return kind in ('', 'file')


def _skill_artifact_needs_wire_gate(source_tool: str, row: dict) -> bool:
    if not _is_skill_manifest_row(row):
        return False
    tool = _normalize_tool_name(source_tool)
    if tool == SKILL_MANAGE_TOOL:
        return True
    return tool in ARTIFACT_MUTATION_TOOLS


def _skill_row_has_provenance(row: dict, collection: str) -> bool:
    source_tool = _normalize_tool_name(str(row.get('source_tool') or '').strip())
    if collection == 'references':
        return source_tool in REFERENCE_SKILL_TOOLS
    if collection == 'artifacts':
        if _skill_artifact_needs_wire_gate(source_tool, row):
            return True
        return str(row.get('preview') or '').strip() == MANIFEST_PREVIEW_SKILL
    return False


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


def _canonical_skill_manifest_path(name: str, skills_dir: Path | None) -> str:
    """Resolve bare name or category/name to skills_dir-relative manifest path."""
    raw_text = str(name or '').strip()
    if not raw_text:
        return ''
    cleaned = _skill_path_string_normalize(raw_text)
    if not cleaned:
        return ''
    if skills_dir is None:
        return cleaned
    from_path = _skill_manifest_name_from_skill_dir_path(raw_text, skills_dir)
    if from_path:
        return from_path
    from_path = _skill_manifest_name_from_skill_dir_path(cleaned, skills_dir)
    if from_path:
        return from_path
    try:
        from integration.skills.local_skills import _find_skill

        root = Path(skills_dir).expanduser().resolve()
        skill_dir, skill_md = _find_skill(cleaned, root)
        if skill_md is not None and skill_dir is not None:
            rel = skill_dir.relative_to(root)
            rel_str = rel.as_posix()
            if rel_str not in ('', '.'):
                return rel_str
    except (ImportError, OSError, ValueError):
        pass
    return cleaned


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


def _merge_knowledge_base_records(
    records: dict[str, dict],
    candidates: list[dict[str, Any]],
) -> None:
    """Merge fork-owned knowledge-base candidates by their public document identity."""
    from integration.knowledge_base.turn_references import merge_reference_rows, reference_key

    for candidate in candidates:
        key = reference_key(candidate)
        if not key or key == '\0':
            continue
        record_key = f'knowledge_base_document\0{key}'
        existing = records.get(record_key)
        merged = merge_reference_rows([existing] if existing else [], [candidate])
        if merged:
            records[record_key] = merged[0]


def _clean_record_keys(rows: list[dict]) -> list[dict]:
    for row in rows:
        for hit in row.get('hits') or []:
            hit.pop('_key', None)
    return rows


def _reference_sort_key(row: dict) -> tuple[str, str, str]:
    if str(row.get('resource_type') or '') == 'knowledge_base_document':
        return (
            'knowledge_base_document',
            str(row.get('kb_name') or ''),
            str(row.get('file_name') or ''),
        )
    return ('skill', str(row.get('path') or ''), '')


def _next_turn_key(messages: list) -> str:
    """通过扫描现存用户消息，返回下一个稳定的 turn key。"""
    max_num = 0
    for msg in messages or []:
        if not isinstance(msg, dict) or msg.get('role') != 'user':
            continue
        key = msg.get('_turn_key', '')
        if key and key.startswith('turn:'):
            try:
                num = int(key.split(':', 1)[1])
                max_num = max(max_num, num)
            except (TypeError, ValueError):
                pass
    return f'turn:{max_num + 1}'


def _ensure_turn_keys(messages: list) -> list:
    """Return a manifest-owned copy without inventing active turn bindings."""
    return copy.deepcopy(list(messages or []))


def _message_turns(messages: list) -> list[dict[str, Any]]:
    from api.compression_anchor import is_context_compression_marker

    message_rows = list(messages or [])
    user_rows = []
    for idx, message in enumerate(message_rows):
        if not isinstance(message, dict) or message.get('role') != 'user':
            continue
        if is_context_compression_marker(message):
            continue
        if _is_synthetic_control_message(message):
            log_control_message("manifest_turn_skip", message)
            continue
        user_rows.append((idx, message))
    has_stable_turn_keys = any(
        str(message.get('_turn_key') or '').strip()
        for _idx, message in user_rows
    )
    turns: list[dict[str, Any]] = []
    for row_idx, (idx, message) in enumerate(user_rows):
        turn_key = str(message.get('_turn_key') or '').strip()
        if not turn_key:
            if has_stable_turn_keys:
                continue
            turn_key = f'turn:{idx}'
        end_msg_idx = (
            user_rows[row_idx + 1][0] - 1
            if row_idx + 1 < len(user_rows)
            else len(message_rows) - 1
        )
        turns.append({
            'turn_key': turn_key,
            'user_msg_idx': idx,
            'start_msg_idx': idx,
            'end_msg_idx': end_msg_idx,
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


def _turn_record_for_key(messages: list, turn_key: str) -> dict[str, Any] | None:
    key = str(turn_key or '').strip()
    if not key:
        return None
    for turn in _message_turns(messages or []):
        if str(turn.get('turn_key') or '') == key:
            return turn
    return None


def _tool_calls_for_turn(
    tool_calls: list | None,
    *,
    start_msg_idx: int | None,
    end_msg_idx: int | None,
) -> list:
    """Keep persisted session tool-call snippets scoped to one transcript turn."""
    if (
        isinstance(start_msg_idx, bool)
        or isinstance(end_msg_idx, bool)
        or not isinstance(start_msg_idx, int)
        or not isinstance(end_msg_idx, int)
    ):
        return []
    scoped: list = []
    for tc in tool_calls or []:
        if not isinstance(tc, dict):
            continue
        assistant_idx = tc.get('assistant_msg_idx')
        if isinstance(assistant_idx, bool) or not isinstance(assistant_idx, int):
            continue
        if start_msg_idx <= assistant_idx <= end_msg_idx:
            scoped.append(tc)
    return scoped


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

    # A replayed transcript can contain the same completed tool call on both
    # sides of a later user boundary. Keep its latest occurrence; recovery
    # restamps replay rows after the current turn and that is the canonical tail.
    execution_turn_owners: dict[str, str | None] = {}
    read_evidence_by_turn: dict[str, set[str]] = {}
    for event in events:
        event_turn_key = _turn_key_for_event(event, turns)
        execution_key = str(event.tid or '').strip()
        if execution_key:
            execution_turn_owners[execution_key] = event_turn_key
        if event.name in ARTIFACT_EXCLUSION_READ_TOOLS and _tool_event_succeeded(event):
            evidence = read_evidence_by_turn.setdefault(str(event_turn_key or ''), set())
            for path in _paths_from_args(event.args, workspace):
                key = _canonical_manifest_file_key(path, workspace)
                if key:
                    evidence.add(key)

    for event in events:
        name = event.name
        tool_succeeded = _tool_event_succeeded(event)
        args_paths: list[str] = []
        diff_paths: list[str] = []
        if name in (MEDIA_ARTIFACT_SOURCE, ASSISTANT_PROSE_ARTIFACT_SOURCE):
            args_paths = _paths_from_args(event.args, workspace)
        elif tool_succeeded:
            args_paths = _paths_from_args(event.args, workspace)
            text_blobs = [event.result] if event.result else []
            if event.args:
                try:
                    text_blobs.append(json.dumps(event.args, ensure_ascii=False))
                except (TypeError, ValueError):
                    pass
            for blob in text_blobs:
                diff_paths.extend(_paths_from_diff_text(blob, workspace))
        turn_key = _turn_key_for_event(event, turns)
        execution_key = str(event.tid or '').strip()
        replayed_execution = bool(
            execution_key and execution_turn_owners.get(execution_key) != turn_key
        )
        turn = turn_rows.get(turn_key or '')

        def add_artifact(path: str) -> None:
            _merge_file_records(
                artifacts, kind='artifact', path=path, event=event, entry_kind='file', workspace=workspace,
            )
            if turn is not None and not replayed_execution:
                _merge_file_records(
                    turn['artifacts'], kind='artifact', path=path, event=event, entry_kind='file', workspace=workspace,
                )

        def add_skill_artifact(path: str) -> None:
            skill_name = _skill_manifest_name_from_skills_path(path, skills_dir)
            if not skill_name:
                return
            _merge_skill_records(artifacts, skill_name=skill_name, event=event)
            if turn is not None and not replayed_execution:
                _merge_skill_records(turn['artifacts'], skill_name=skill_name, event=event)

        if name in ARTIFACT_MUTATION_TOOLS and _tool_event_succeeded(event):
            artifact_paths = args_paths + diff_paths
            artifact_paths.extend(_paths_from_diff_text(event.result, workspace))
            for path in artifact_paths:
                if _skill_manifest_name_from_skills_path(path, skills_dir):
                    add_skill_artifact(path)
                else:
                    add_artifact(path)

        if name in EXECUTION_ARTIFACT_TOOLS:
            for path in _execution_artifact_paths(event, workspace):
                add_artifact(path)

        if name in (MEDIA_ARTIFACT_SOURCE, ASSISTANT_PROSE_ARTIFACT_SOURCE):
            for path in args_paths:
                if (
                    name == ASSISTANT_PROSE_ARTIFACT_SOURCE
                    and _canonical_manifest_file_key(path, workspace)
                    in read_evidence_by_turn.get(str(turn_key or ''), set())
                ):
                    continue
                add_artifact(path)

        if name in REFERENCE_SKILL_TOOLS:
            skill_name = _skill_manifest_name_from_view_event(event, skills_dir)
            if skill_name:
                canonical = _canonical_skill_manifest_path(skill_name, skills_dir)
                if canonical:
                    # Only skill artifact rows participate in reference dedupe.
                    # Canonicalizing every file artifact key (old behavior) forces
                    # a full skills-dir scan/miss per key and dominates GET latency.
                    session_skill_keys = _skill_canonical_keys_from_rows(
                        list(artifacts.values()), skills_dir,
                    )
                    turn_skill_keys = set()
                    if turn is not None:
                        turn_skill_keys = _skill_canonical_keys_from_rows(
                            list((turn.get('artifacts') or {}).values()),
                            skills_dir,
                        )
                    if canonical in session_skill_keys or canonical in turn_skill_keys:
                        continue
                    skill_name = canonical
                _merge_skill_records(references, skill_name=skill_name, event=event)
                if turn is not None:
                    _merge_skill_records(turn['references'], skill_name=skill_name, event=event)

        if tool_succeeded:
            try:
                from integration.knowledge_base.turn_references import extract_references

                knowledge_base_references = extract_references(
                    name=event.name,
                    args=event.args,
                    result=event.result,
                    status=event.status,
                    tid=event.tid,
                )
            except Exception:
                logger.debug('failed to extract knowledge-base turn references', exc_info=True)
                knowledge_base_references = []
            if knowledge_base_references:
                _merge_knowledge_base_records(references, knowledge_base_references)
                if turn is not None and not replayed_execution:
                    _merge_knowledge_base_records(turn['references'], knowledge_base_references)

        if _is_skill_manage_mutation_event(event) and _tool_event_succeeded(event):
            skill_name = _skill_manifest_name_from_manage_event(event, skills_dir)
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
    reference_list = sorted(references.values(), key=_reference_sort_key)
    _clean_record_keys(artifact_list + reference_list)
    turn_list: list[dict] = []
    for turn in turn_rows.values():
        turn_artifacts = sorted(turn.get('artifacts', {}).values(), key=lambda row: row['path'])
        turn_references = sorted(turn.get('references', {}).values(), key=_reference_sort_key)
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


def _artifact_path_is_real(workspace: Path, rel: str) -> bool:
    """True when rel resolves to an existing previewable workspace file."""
    return _file_preview_path(workspace, rel, 'file') is not None


def filter_existing_turn_artifact_entries(
    workspace: Path,
    skills_dir: Path | None,
    entries: list[dict[str, str]] | None,
) -> list[dict[str, str]]:
    """Keep only previewable file and skill artifact entries."""
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        path = str(entry.get('path') or '').strip()
        if not path or path in seen:
            continue
        preview = str(entry.get('preview') or MANIFEST_PREVIEW_FILE).strip()
        source_tool = str(entry.get('source_tool') or '').strip()
        if preview == MANIFEST_PREVIEW_SKILL:
            if not _skillhub_preview_available():
                continue
            canonical = _canonical_skill_manifest_path(path, skills_dir)
            if not canonical or not _skill_exists_in_dir(skills_dir, canonical):
                continue
            path = canonical
        elif source_tool == MEDIA_ARTIFACT_SOURCE:
            if not _artifact_path_is_real(workspace, path) and not _session_media_preview_path(workspace, path, MANIFEST_PREVIEW_FILE):
                continue
        elif not _artifact_path_is_real(workspace, path):
            continue
        seen.add(path)
        out.append({
            'path': path,
            'source_tool': source_tool,
            'preview': preview if preview == MANIFEST_PREVIEW_SKILL else MANIFEST_PREVIEW_FILE,
        })
    return out


def filter_existing_turn_artifact_paths(
    workspace: Path,
    paths: list[str] | None,
) -> list[str]:
    """Keep only workspace paths that pass the manifest preview gate."""
    entries = [
        {'path': path, 'source_tool': 'write_file', 'preview': MANIFEST_PREVIEW_FILE}
        for path in (paths or [])
        if isinstance(path, str) and path.strip()
    ]
    filtered = filter_existing_turn_artifact_entries(workspace, None, entries)
    return [entry['path'] for entry in filtered]


def _normalize_persisted_turn_artifact_entry(entry: Any) -> dict[str, str] | None:
    if isinstance(entry, str):
        path = entry.strip()
        if not path:
            return None
        return {
            'path': path,
            'source_tool': 'write_file',
            'preview': MANIFEST_PREVIEW_FILE,
        }
    if isinstance(entry, dict):
        path = str(entry.get('path') or '').strip()
        if not path:
            return None
        preview = str(entry.get('preview') or MANIFEST_PREVIEW_FILE).strip()
        source_tool = str(entry.get('source_tool') or '').strip()
        if preview != MANIFEST_PREVIEW_SKILL:
            preview = MANIFEST_PREVIEW_FILE
        if not source_tool:
            source_tool = 'write_file'
        return {'path': path, 'source_tool': source_tool, 'preview': preview}
    return None


def _artifact_record_from_persisted_entry(
    entry: dict[str, str],
    *,
    turn_key: str,
    default_profile: str,
    artifact_records: dict[str, dict],
    skills_dir: Path | None,
) -> dict[str, Any] | None:
    path = str(entry.get('path') or '').strip()
    source_tool = str(entry.get('source_tool') or '').strip() or 'write_file'
    preview = str(entry.get('preview') or MANIFEST_PREVIEW_FILE).strip()
    if not path:
        return None
    if preview == MANIFEST_PREVIEW_SKILL:
        path = _canonical_skill_manifest_path(path, skills_dir)
        if not path:
            return None
        if not source_tool and path in artifact_records:
            source_tool = str(artifact_records[path].get('source_tool') or '').strip() or SKILL_MANAGE_TOOL
        record: dict[str, Any] = {
            'path': path,
            'source_tool': source_tool,
            'kind': 'skill',
            'entry_kind': 'skill',
            'resource_type': 'skill',
            'skill_name': path,
            'preview': MANIFEST_PREVIEW_SKILL,
            'profile': default_profile,
            'turn_key': str(turn_key or '').strip(),
        }
        return record
    if not source_tool and path in artifact_records:
        source_tool = str(artifact_records[path].get('source_tool') or '').strip()
    if not source_tool:
        source_tool = 'write_file'
    return {
        'path': path,
        'source_tool': source_tool,
        'kind': 'artifact',
        'entry_kind': 'file',
        'preview': MANIFEST_PREVIEW_FILE,
        'profile': default_profile,
        'turn_key': str(turn_key or '').strip(),
    }


def _merge_persisted_turn_artifact_records(
    turn: dict[str, Any],
    entries: list,
    *,
    default_profile: str,
    artifact_records: dict[str, dict],
    skills_dir: Path | None,
) -> None:
    turn_key = str(turn.get('turn_key') or '').strip()
    transcript_skills = [
        row for row in list(turn.get('artifacts') or [])
        if isinstance(row, dict) and _is_skill_manifest_row(row)
    ]
    persisted_rows: list[dict[str, Any]] = []
    for raw_entry in entries:
        normalized = _normalize_persisted_turn_artifact_entry(raw_entry)
        if normalized is None:
            continue
        record = _artifact_record_from_persisted_entry(
            normalized,
            turn_key=turn_key,
            default_profile=default_profile,
            artifact_records=artifact_records,
            skills_dir=skills_dir,
        )
        if record is not None:
            persisted_rows.append(record)

    merged: dict[str, dict[str, Any]] = {}
    for row in persisted_rows:
        merged[str(row.get('path') or '')] = row
    for row in transcript_skills:
        canonical = _canonical_skill_manifest_path(str(row.get('path') or ''), skills_dir)
        if canonical and canonical not in merged:
            merged[canonical] = {**row, 'path': canonical}
    turn['artifacts'] = sorted(merged.values(), key=lambda row: str(row.get('path') or ''))
    for row in turn['artifacts']:
        path = str(row.get('path') or '').strip()
        if path and path not in artifact_records:
            artifact_records[path] = row


def _skill_canonical_keys_from_rows(
    rows: list[dict] | None,
    skills_dir: Path | None,
) -> set[str]:
    keys: set[str] = set()
    for row in rows or []:
        if not isinstance(row, dict) or not _is_skill_manifest_row(row):
            continue
        path = str(row.get('path') or row.get('skill_name') or '').strip()
        if not path:
            continue
        canonical = _canonical_skill_manifest_path(path, skills_dir)
        if canonical:
            keys.add(canonical)
    return keys


def _drop_reference_skills_in_artifacts(
    artifacts: list[dict],
    references: list[dict],
    turns: list[dict],
    skills_dir: Path | None,
) -> None:
    artifact_skill_keys = _skill_canonical_keys_from_rows(artifacts, skills_dir)
    for turn in turns:
        artifact_skill_keys |= _skill_canonical_keys_from_rows(turn.get('artifacts'), skills_dir)
    if not artifact_skill_keys:
        return

    def should_drop(row: dict) -> bool:
        if not isinstance(row, dict) or not _is_skill_manifest_row(row):
            return False
        path = str(row.get('path') or '').strip()
        if not path:
            return False
        return _canonical_skill_manifest_path(path, skills_dir) in artifact_skill_keys

    references[:] = [row for row in references if not should_drop(row)]
    for turn in turns:
        turn_refs = turn.get('references')
        if isinstance(turn_refs, list):
            turn['references'] = [row for row in turn_refs if not should_drop(row)]


def turn_artifacts_for_wire(session) -> dict[str, list[dict[str, str]]]:
    """Return legacy turn_artifacts with only existing previewable entries."""
    workspace = Path(str(getattr(session, 'workspace', '') or '')).expanduser().resolve()
    artifact_workspace = artifact_workspace_root_for_session(session)
    skills_dir = _skills_dir_for_session(session)
    raw = getattr(session, 'turn_artifacts', None) or {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, list[dict[str, str]]] = {}
    for turn_key, entries in raw.items():
        tk = str(turn_key or '').strip()
        if not tk or not isinstance(entries, list):
            continue
        normalized: list[dict[str, str]] = []
        for entry in entries:
            row = _normalize_persisted_turn_artifact_entry(entry)
            if row is not None:
                normalized.append(row)
        rebased = _rebase_file_artifact_records(normalized, workspace, artifact_workspace)
        filtered = filter_existing_turn_artifact_entries(artifact_workspace, skills_dir, rebased)
        if filtered:
            out[tk] = filtered
    return out


def extract_turn_artifact_entries_for_manifest(
    session,
    turn_key: str,
    *,
    prior_artifact_paths: list[str] | tuple[str, ...] | set[str] = (),
) -> list[dict[str, str]]:
    """Extract previewable artifact entries for one turn using the shared manifest rules."""
    messages = list(getattr(session, 'messages', None) or [])
    key = str(turn_key or '').strip()
    if not messages or not key:
        return []
    turn_slice = _turn_message_slice(messages, key)
    if not turn_slice:
        return []
    turn_bounds = next(
        (
            (turn.get('start_msg_idx'), turn.get('end_msg_idx'))
            for turn in _message_turns(messages)
            if str(turn.get('turn_key') or '') == key
        ),
        None,
    )
    workspace = Path(str(getattr(session, 'workspace', '') or '')).expanduser().resolve()
    artifact_workspace = artifact_workspace_root_for_session(session)
    skills_dir = _skills_dir_for_session(session)
    all_tool_calls = getattr(session, 'tool_calls', None)
    accumulated_prior = list(prior_artifact_paths)
    entries: list[dict[str, str]] = []
    for turn in _message_turns(messages):
        current_key = str(turn.get('turn_key') or '').strip()
        if not current_key:
            continue
        current_messages = _turn_message_slice(messages, current_key)
        current_entries = _extract_turn_artifact_entries(
            current_messages,
            all_tool_calls,
            workspace,
            start_msg_idx=turn.get('start_msg_idx'),
            end_msg_idx=turn.get('end_msg_idx'),
            skills_dir=skills_dir,
            prior_artifact_paths=accumulated_prior,
        )
        rebased_entries = _rebase_file_artifact_records(
            current_entries, workspace, artifact_workspace,
        )
        filtered_entries = filter_existing_turn_artifact_entries(
            artifact_workspace, skills_dir, rebased_entries,
        )
        if current_key == key:
            entries = filtered_entries
            break
        accumulated_prior.extend(
            str(entry.get('path') or '').strip()
            for entry in filtered_entries
            if str(entry.get('path') or '').strip()
        )
    return entries


def _turn_message_slice(messages: list, turn_key: str) -> list:
    key = str(turn_key or '').strip()
    for turn in _message_turns(messages or []):
        if str(turn.get('turn_key') or '') == key:
            start = int(turn.get('start_msg_idx', 0))
            end = int(turn.get('end_msg_idx', len(messages or []) - 1))
            return list(messages[start:end + 1])
    return []


def _records_by_path(rows: list[dict] | None) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        path = str(row.get('path') or '').strip()
        if path:
            out[path] = row
    return out


def _artifact_identity(
    row: dict,
    default_profile: str = '',
    default_workspace_root: str = '',
) -> str:
    profile = str(row.get('profile') if 'profile' in row else default_profile or '').strip()
    workspace_root = str(
        row.get('workspace_root') if 'workspace_root' in row else default_workspace_root or ''
    ).strip()
    path = str(row.get('path') or '').strip()
    return f'{profile}\0{workspace_root}\0{path}'


def _store_artifact_record(row: dict[str, Any]) -> dict[str, Any] | None:
    path = str(row.get('path') or '').strip()
    source_tool = str(row.get('source_tool') or '').strip()
    if not path or not source_tool:
        return None
    profile = str(row.get('profile') or '').strip()
    preview = str(row.get('preview') or MANIFEST_PREVIEW_FILE).strip() or MANIFEST_PREVIEW_FILE
    is_skill = preview == MANIFEST_PREVIEW_SKILL
    record: dict[str, Any] = {
        'path': path,
        'source_tool': source_tool,
        'kind': 'skill' if is_skill else 'artifact',
        'entry_kind': 'skill' if is_skill else 'file',
        'preview': preview,
        'profile': profile,
        'turn_key': str(row.get('turn_key') or '').strip(),
    }
    # Store reads always carry this field, including legacy ``''`` rows.  Keep
    # unpersisted reconcile rows unscoped so their pre-existing SSE behavior is
    # unchanged; the durable store is where root identity becomes mandatory.
    if 'workspace_root' in row:
        record['workspace_root'] = str(row.get('workspace_root') or '').strip()
    if is_skill:
        record['resource_type'] = 'skill'
        record['skill_name'] = path
    return record


def _is_reference_only_tool(name: str) -> bool:
    return name in _REFERENCE_ONLY_TOOLS


def _reconcile_candidate_paths(event: ToolEvent, workspace: Path) -> list[str]:
    """Collect workspace-relative candidate paths for turn reconcile extraction."""
    name = event.name
    if name in ARTIFACT_MUTATION_TOOLS or _is_skill_manage_mutation_event(event):
        return []
    args_paths = _paths_from_args(event.args, workspace)
    if name == MEDIA_ARTIFACT_SOURCE:
        return args_paths
    if name == ASSISTANT_PROSE_ARTIFACT_SOURCE:
        return args_paths
    if name in EXECUTION_ARTIFACT_TOOLS:
        return _execution_artifact_paths(event, workspace)
    if name in ARTIFACT_EXCLUSION_READ_TOOLS:
        return []
    if _is_reference_only_tool(name):
        return []
    return []


def _merge_reconcile_artifacts_for_turn(
    artifact_records: dict[str, dict],
    turn_record: dict[str, Any],
    messages: list,
    workspace: Path,
    *,
    skills_dir: Path | None = None,
    tool_calls: list | None = None,
    prior_artifact_paths: list[str] | tuple[str, ...] | set[str] = (),
) -> None:
    """Merge transcript-mined artifact candidates for one turn into record dicts."""
    turn_key = str(turn_record.get('turn_key') or '').strip()
    if not turn_key:
        return
    turn_messages = _turn_message_slice(messages, turn_key)
    if not turn_messages:
        return

    turn_artifact_records = _records_by_path(turn_record.get('artifacts'))
    scoped_tool_calls = _tool_calls_for_turn(
        tool_calls,
        start_msg_idx=turn_record.get('start_msg_idx'),
        end_msg_idx=turn_record.get('end_msg_idx'),
    )
    events = _collect_tool_events(turn_messages, scoped_tool_calls)
    # turn_messages is already sliced; scan all assistant rows in the slice (not global turn_key).
    events.extend(_collect_media_artifact_events(turn_messages, workspace))
    strong_entries = _collect_turn_artifact_entries_from_events(events, workspace, skills_dir=skills_dir)
    strong_paths = [
        str(entry.get('path') or '').strip()
        for entry in strong_entries
        if isinstance(entry, dict) and str(entry.get('preview') or MANIFEST_PREVIEW_FILE) == MANIFEST_PREVIEW_FILE
    ]
    events.extend(_collect_final_assistant_artifact_events(
        turn_messages,
        workspace,
        strong_paths=strong_paths,
        prior_artifact_paths=prior_artifact_paths,
    ))
    read_evidence_keys = {
        key
        for event in events
        if event.name in ARTIFACT_EXCLUSION_READ_TOOLS and _tool_event_succeeded(event)
        for path in _paths_from_args(event.args, workspace)
        for key in [_canonical_manifest_file_key(path, workspace)]
        if key
    }
    local_turn_record = {
        'turn_key': turn_key,
        'start_msg_idx': 0,
        'end_msg_idx': max(0, len(turn_messages) - 1),
    }

    for event in events:
        event_turn_record = turn_record if event.source == 'session_tool_calls' else local_turn_record
        if _turn_key_for_event(event, [event_turn_record]) != turn_key:
            continue
        if event.name == 'todo':
            continue
        for path in _reconcile_candidate_paths(event, workspace):
            if (
                event.name == ASSISTANT_PROSE_ARTIFACT_SOURCE
                and _canonical_manifest_file_key(path, workspace) in read_evidence_keys
            ):
                continue
            if not _artifact_path_is_real(workspace, path):
                continue
            skill_name = _skill_manifest_name_from_skills_path(path, skills_dir)
            if skill_name:
                _merge_skill_records(artifact_records, skill_name=skill_name, event=event)
                _merge_skill_records(turn_artifact_records, skill_name=skill_name, event=event)
            else:
                _merge_file_records(
                    artifact_records,
                    kind='artifact',
                    path=path,
                    event=event,
                    entry_kind='file',
                    workspace=workspace,
                )
                _merge_file_records(
                    turn_artifact_records,
                    kind='artifact',
                    path=path,
                    event=event,
                    entry_kind='file',
                    workspace=workspace,
                )

    turn_artifacts = sorted(turn_artifact_records.values(), key=lambda row: row['path'])
    _clean_record_keys(turn_artifacts)
    turn_record['artifacts'] = turn_artifacts


def _apply_turn_reconcile_to_manifest_records(
    artifact_records: dict[str, dict],
    turn_records: list[dict[str, Any]],
    messages: list,
    workspace: Path,
    *,
    skills_dir: Path | None = None,
    tool_calls: list | None = None,
    skip_turn_keys: set[str] | None = None,
) -> None:
    skip = {str(key or '').strip() for key in (skip_turn_keys or set()) if str(key or '').strip()}
    prior_artifact_paths: list[str] = []
    for turn in turn_records:
        turn_key = str(turn.get('turn_key') or '').strip()
        if turn_key in skip:
            prior_artifact_paths.extend(
                str(row.get('path') or '').strip()
                for row in (turn.get('artifacts') or [])
                if isinstance(row, dict) and str(row.get('path') or '').strip()
            )
            continue
        _merge_reconcile_artifacts_for_turn(
            artifact_records,
            turn,
            messages,
            workspace,
            skills_dir=skills_dir,
            tool_calls=tool_calls,
            prior_artifact_paths=prior_artifact_paths,
        )
        prior_artifact_paths.extend(
            str(row.get('path') or '').strip()
            for row in (turn.get('artifacts') or [])
            if isinstance(row, dict) and str(row.get('path') or '').strip()
        )


def reconcile_turn_artifact_events(
    messages: list,
    workspace: Path,
    turn_key: str,
    *,
    skills_dir: Path | None = None,
    tool_calls: list | None = None,
) -> list[ToolEvent]:
    """Return tool/media events for one turn (inputs to reconcile extraction)."""
    turn_messages = _turn_message_slice(messages, turn_key)
    if not turn_messages:
        return []
    turn_record = _turn_record_for_key(messages, turn_key)
    scoped_tool_calls = _tool_calls_for_turn(
        tool_calls,
        start_msg_idx=turn_record.get('start_msg_idx') if turn_record else None,
        end_msg_idx=turn_record.get('end_msg_idx') if turn_record else None,
    )
    events = _collect_tool_events(turn_messages, scoped_tool_calls)
    events.extend(_collect_media_artifact_events(turn_messages, workspace))
    events.extend(_collect_final_assistant_artifact_events(turn_messages, workspace))
    return events


def _reconcile_turn_artifact_rows(
    messages: list,
    workspace: Path,
    turn_key: str,
    *,
    skills_dir: Path | None = None,
    tool_calls: list | None = None,
) -> tuple[list[dict], list[dict]]:
    """Build reconciled artifact rows for one turn (pre-wire internal records)."""
    turn_messages = _turn_message_slice(messages, turn_key)
    if not turn_messages:
        return [], []
    turns = _message_turns(messages)
    turn_record: dict[str, Any] = {
        'turn_key': turn_key,
        'artifacts': [],
        'references': [],
    }
    for turn in turns:
        if str(turn.get('turn_key') or '') == turn_key:
            turn_record = {
                'turn_key': turn_key,
                'user_msg_idx': turn.get('user_msg_idx'),
                'start_msg_idx': turn.get('start_msg_idx'),
                'end_msg_idx': turn.get('end_msg_idx'),
                'artifacts': [],
                'references': [],
            }
            break
    artifact_records: dict[str, dict] = {}
    prior_artifact_paths: list[str] = []
    for turn in turns:
        current_key = str(turn.get('turn_key') or '').strip()
        if not current_key:
            continue
        current_record = turn_record if current_key == turn_key else {
            **turn,
            'artifacts': [],
            'references': [],
        }
        current_records = artifact_records if current_key == turn_key else {}
        _merge_reconcile_artifacts_for_turn(
            current_records,
            current_record,
            messages,
            workspace,
            skills_dir=skills_dir,
            tool_calls=tool_calls,
            prior_artifact_paths=prior_artifact_paths,
        )
        prior_artifact_paths.extend(
            str(row.get('path') or '').strip()
            for row in (current_record.get('artifacts') or [])
            if isinstance(row, dict) and str(row.get('path') or '').strip()
        )
        if current_key == turn_key:
            turn_record = current_record
            break
    artifact_list = sorted(artifact_records.values(), key=lambda row: row['path'])
    _clean_record_keys(artifact_list)
    return artifact_list, list(turn_record.get('artifacts') or [])


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


def _serialize_manifest_row(
    path: str,
    preview: str,
    source_tool: str,
    *,
    profile: str = '',
    status: str = '',
) -> dict[str, str]:
    row = {
        'path': path,
        'preview': preview,
        'source_tool': source_tool,
    }
    profile_text = str(profile or '').strip()
    if profile_text:
        row['profile'] = profile_text
    status_text = str(status or '').strip()
    if status_text:
        row['status'] = status_text
    return row


def _wire_file_path_for_integration(workspace_root: Path, path: str) -> str:
    """Project session-relative file paths onto DEFAULT_WORKSPACE for wire/SSE.

    Matches left-rail ``/api/integration/workspace/files`` prefixing so
    ``/api/integration/workspace/file`` can open managed-session artifacts.
    """
    from api.session_manifest_store import project_artifact_path_for_integration_root

    return project_artifact_path_for_integration_root(path, workspace_root)


def _expired_workspace_file_wire_path(workspace: Path, rel: str, entry_kind: str) -> str | None:
    if entry_kind == 'dir':
        return None
    ws_rel, in_workspace = _workspace_relative_path(workspace, rel)
    if not in_workspace:
        return None
    from api.workspace import is_workspace_cruft_basename

    if is_workspace_cruft_basename(Path(ws_rel).name):
        return None
    return ws_rel


def _expired_media_file_wire_path(workspace: Path, rel: str, entry_kind: str) -> str | None:
    if entry_kind == 'dir':
        return None
    _ws_rel, in_workspace = _workspace_relative_path(workspace, rel)
    if in_workspace:
        return None
    path_text = str(rel or '').strip()
    if not path_text:
        return None
    try:
        candidate = Path(path_text).expanduser()
        if not candidate.is_absolute():
            return None
        return candidate.as_posix()
    except (ValueError, OSError):
        return None


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
        from api.workspace import is_workspace_cruft_basename

        target = Path(path_text).expanduser().resolve()
        if not target.is_file():
            return None
        if is_workspace_cruft_basename(target.name):
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
        from api.workspace import is_workspace_cruft_basename, safe_resolve_ws

        target = safe_resolve_ws(workspace, ws_rel)
        if not target.is_file():
            return None
        if is_workspace_cruft_basename(target.name):
            return None
        return ws_rel
    except (ValueError, OSError, FileNotFoundError):
        return None


def _row_to_wire(
    row: dict,
    workspace: Path,
    skills_dir: Path | None = None,
    *,
    default_profile: str = '',
    collection: str = 'artifacts',
) -> dict[str, Any] | None:
    if collection == 'references' and str(row.get('resource_type') or '') == 'knowledge_base_document':
        from integration.knowledge_base.turn_references import to_wire

        return to_wire(row)
    source_tool = str(row.get('source_tool') or '').strip()
    if not source_tool:
        return None
    if 'profile' in row:
        profile = str(row.get('profile') or '').strip()
    else:
        profile = str(default_profile or '').strip()
    is_skill_row = _is_skill_manifest_row(row)
    if collection == 'references' and (
        not is_skill_row or _normalize_tool_name(source_tool) not in REFERENCE_SKILL_TOOLS
    ):
        return None
    if is_skill_row:
        skill_name = str(row.get('skill_name') or row.get('path') or '').strip()
        if not skill_name or not _skillhub_preview_available():
            return None
        if str(row.get('status') or '').strip().lower() == 'in_progress':
            return None
        if collection == 'references':
            sources: list[dict[str, str]] = []
            seen_sources: set[tuple[str, str]] = set()
            for hit in [row, *(row.get('hits') or [])]:
                if not isinstance(hit, dict):
                    continue
                tool = str(hit.get('source_tool') or '').strip()
                tid = str(hit.get('tid') or '').strip()
                key = (tool, tid)
                if tool and key not in seen_sources:
                    sources.append({'tool': tool, 'tid': tid})
                    seen_sources.add(key)
            if not sources:
                return None
            wire: dict[str, Any] = {
                'kind': 'skill',
                'source': sources,
                'metadata': {'path': skill_name},
            }
            if not _skill_exists_in_dir(skills_dir, skill_name):
                if not _skill_row_has_provenance(row, collection):
                    return None
                wire['status'] = MANIFEST_STATUS_EXPIRED
            return wire
        if _skill_exists_in_dir(skills_dir, skill_name):
            return _serialize_manifest_row(
                skill_name, MANIFEST_PREVIEW_SKILL, source_tool, profile=profile,
            )
        if _skill_row_has_provenance(row, collection):
            return _serialize_manifest_row(
                skill_name,
                MANIFEST_PREVIEW_SKILL,
                source_tool,
                profile=profile,
                status=MANIFEST_STATUS_EXPIRED,
            )
        return None
    rel = str(row.get('path') or '').strip()
    entry_kind = str(row.get('kind') or row.get('entry_kind') or 'file')
    try:
        from api.session_manifest_store import (
            effective_manifest_workspace_root,
            relative_prefix_under_root,
        )

        root_scoped = 'workspace_root' in row
        stored_root = row.get('workspace_root') if root_scoped else workspace
        row_workspace = effective_manifest_workspace_root(stored_root)
    except Exception:
        row_workspace = None
    if row_workspace is None:
        return None
    integration_prefix = relative_prefix_under_root(row_workspace)
    preview_path = _file_preview_path(row_workspace, rel, entry_kind)
    if preview_path:
        # The existing workspace file endpoint is rooted at DEFAULT_WORKSPACE.
        # A record outside that root has no safe locator in its current wire
        # shape, so never fall back to a bare relative path.
        if root_scoped and integration_prefix is None:
            return None
        return _serialize_manifest_row(
            _wire_file_path_for_integration(row_workspace, preview_path),
            MANIFEST_PREVIEW_FILE,
            source_tool,
            profile=profile,
        )
    if source_tool == MEDIA_ARTIFACT_SOURCE:
        media_path = _session_media_preview_path(row_workspace, rel, entry_kind)
        if media_path:
            # Absolute MEDIA paths stay absolute; in-workspace MEDIA already
            # returned via _file_preview_path above.
            return _serialize_manifest_row(
                media_path, MANIFEST_PREVIEW_FILE, source_tool, profile=profile,
            )
    if collection == 'artifacts':
        # Scheme A: only emit expired rows when the artifact has per-turn
        # provenance (store/turn_artifacts). This avoids surfacing
        # unattributed global candidates in session-level artifacts.
        turn_key = str(row.get('turn_key') or '').strip()
        if not turn_key:
            return None
        expired_path = None
        if source_tool == MEDIA_ARTIFACT_SOURCE:
            expired_path = _expired_media_file_wire_path(row_workspace, rel, entry_kind)
        else:
            if root_scoped and integration_prefix is None:
                return None
            expired_path = _expired_workspace_file_wire_path(row_workspace, rel, entry_kind)
            if expired_path:
                expired_path = _wire_file_path_for_integration(row_workspace, expired_path)
        if expired_path:
            return _serialize_manifest_row(
                expired_path,
                MANIFEST_PREVIEW_FILE,
                source_tool,
                profile=profile,
                status=MANIFEST_STATUS_EXPIRED,
            )
    return None


def _rows_to_wire(
    rows: list[dict] | None,
    workspace: Path,
    skills_dir: Path | None = None,
    *,
    default_profile: str = '',
    collection: str = 'artifacts',
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows or []:
        wire = _row_to_wire(
            row,
            workspace,
            skills_dir,
            default_profile=default_profile,
            collection=collection,
        )
        if wire is None:
            continue
        if collection == 'references':
            kind = str(wire.get('kind') or '').strip()
            metadata = wire.get('metadata') if isinstance(wire.get('metadata'), dict) else {}
            if kind == 'skill':
                key = f'skill\0{str(metadata.get("path") or "").strip()}'
            elif kind == 'knowledge_base_document':
                key = 'knowledge_base_document\0{}\0{}'.format(
                    str(metadata.get('kbName') or '').strip(),
                    str(metadata.get('fileName') or '').strip(),
                )
            else:
                continue
            if key in seen:
                continue
            seen.add(key)
            out.append(wire)
            continue
        path = wire['path']
        profile = str(wire.get('profile') or '').strip()
        key = f'{profile}\0{path}'
        if key in seen:
            continue
        seen.add(key)
        out.append(wire)
    if collection == 'references':
        return sorted(
            out,
            key=lambda item: (
                str(item.get('kind') or ''),
                str((item.get('metadata') or {}).get('kbName') or (item.get('metadata') or {}).get('path') or ''),
                str((item.get('metadata') or {}).get('fileName') or ''),
            ),
        )
    return sorted(out, key=lambda item: (str(item.get('profile') or ''), item['path']))


def _turn_to_wire(
    turn: dict,
    workspace: Path,
    skills_dir: Path | None = None,
    *,
    default_profile: str = '',
) -> dict[str, Any]:
    return {
        'turn_key': str(turn.get('turn_key') or ''),
        'artifacts': _rows_to_wire(
            turn.get('artifacts'), workspace, skills_dir,
            default_profile=default_profile, collection='artifacts',
        ),
        'references': _rows_to_wire(
            turn.get('references'), workspace, skills_dir, collection='references',
        ),
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


def _merge_rows_by_identity(existing_rows: list | None, incoming_rows: list | None) -> list[dict]:
    rows: dict[str, dict] = {}
    for row in list(existing_rows or []) + list(incoming_rows or []):
        if not isinstance(row, dict):
            continue
        path = str(row.get('path') or '').strip()
        preview = str(row.get('preview') or '').strip()
        source_tool = str(row.get('source_tool') or '').strip()
        if not path or preview not in (MANIFEST_PREVIEW_FILE, MANIFEST_PREVIEW_SKILL) or not source_tool:
            continue
        profile = str(row.get('profile') or '').strip()
        key = f'{profile}\0{path}'
        merged = _serialize_manifest_row(path, preview, source_tool, profile=profile)
        status = str(row.get('status') or '').strip()
        if status:
            merged['status'] = status
        rows[key] = merged
    return sorted(rows.values(), key=lambda item: (str(item.get('profile') or ''), item['path']))


def _merge_rows_by_path(existing_rows: list | None, incoming_rows: list | None) -> list[dict]:
    return _merge_rows_by_identity(existing_rows, incoming_rows)


def _merge_reference_wire_rows(existing_rows: list | None, incoming_rows: list | None) -> list[dict]:
    """Merge the public reference wire without treating it as a file row."""
    rows: dict[str, dict] = {}
    for row in list(existing_rows or []) + list(incoming_rows or []):
        if not isinstance(row, dict):
            continue
        kind = str(row.get('kind') or '').strip()
        metadata = row.get('metadata') if isinstance(row.get('metadata'), dict) else {}
        if kind == 'skill':
            path = str(metadata.get('path') or '').strip()
            if not path:
                continue
            key = f'skill\0{path}'
            current = rows.setdefault(key, {'kind': 'skill', 'source': [], 'metadata': {'path': path}})
        elif kind == 'knowledge_base_document':
            kb_name = str(metadata.get('kbName') or '').strip()
            file_name = str(metadata.get('fileName') or '').strip()
            if not kb_name or not file_name:
                continue
            key = f'knowledge_base_document\0{kb_name}\0{file_name}'
            current = rows.setdefault(key, {
                'kind': 'knowledge_base_document',
                'source': [],
                'metadata': {'kbName': kb_name, 'fileName': file_name, 'page_content': []},
            })
            contents = current['metadata']['page_content']
            for content in metadata.get('page_content') or []:
                if isinstance(content, str) and content and content not in contents:
                    contents.append(content)
        else:
            continue
        source_keys = {
            (str(source.get('tool') or '').strip(), str(source.get('tid') or '').strip())
            for source in current['source']
            if isinstance(source, dict)
        }
        for source in row.get('source') or []:
            if not isinstance(source, dict):
                continue
            tool = str(source.get('tool') or '').strip()
            tid = str(source.get('tid') or '').strip()
            source_key = (tool, tid)
            if tool and source_key not in source_keys:
                current['source'].append({'tool': tool, 'tid': tid})
                source_keys.add(source_key)
        if str(row.get('status') or '').strip() == MANIFEST_STATUS_EXPIRED:
            current['status'] = MANIFEST_STATUS_EXPIRED
    return sorted(
        rows.values(),
        key=lambda row: (
            str(row.get('kind') or ''),
            str((row.get('metadata') or {}).get('kbName') or (row.get('metadata') or {}).get('path') or ''),
            str((row.get('metadata') or {}).get('fileName') or ''),
        ),
    )


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
            'references': _merge_reference_wire_rows(current.get('references'), turn.get('references')),
        }
    return sorted(turns.values(), key=lambda turn: _turn_sort_key(turn.get('turn_key')))


def _drop_wire_skill_references_in_artifacts(manifest: dict[str, Any]) -> None:
    artifact_keys = {
        str(row.get('path') or '').strip()
        for row in manifest.get('artifacts') or []
        if isinstance(row, dict) and row.get('preview') == MANIFEST_PREVIEW_SKILL
    }
    def is_artifact_skill_reference(row: dict, keys: set[str]) -> bool:
        if not isinstance(row, dict) or row.get('kind') != 'skill':
            return False
        metadata = row.get('metadata') if isinstance(row.get('metadata'), dict) else {}
        return str(metadata.get('path') or '').strip() in keys

    manifest['references'] = [
        row for row in manifest.get('references') or []
        if not is_artifact_skill_reference(row, artifact_keys)
    ]
    for turn in manifest.get('turns') or []:
        if not isinstance(turn, dict):
            continue
        turn_artifact_keys = {
            str(row.get('path') or '').strip()
            for row in turn.get('artifacts') or []
            if isinstance(row, dict) and row.get('preview') == MANIFEST_PREVIEW_SKILL
        }
        turn['references'] = [
            row for row in turn.get('references') or []
            if not is_artifact_skill_reference(row, turn_artifact_keys)
        ]


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
    base_manifest['references'] = _merge_reference_wire_rows(base_manifest.get('references'), delta.get('references'))
    incoming_turns = delta.get('turns')
    if not incoming_turns and delta.get('turn_key'):
        incoming_turns = [{
            'turn_key': delta.get('turn_key'),
            'artifacts': delta.get('artifacts') or [],
            'references': delta.get('references') or [],
        }]
    base_manifest['turns'] = _merge_turn_rows(base_manifest.get('turns'), incoming_turns)
    _drop_wire_skill_references_in_artifacts(base_manifest)
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
    default_profile: str = '',
    artifact_workspace: Path | None = None,
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
    if normalized_event.status != 'completed':
        artifacts, references = [], []
    else:
        artifacts, references = _extract_artifacts_and_references(
            [normalized_event], workspace, skills_dir=skills_dir,
        )
    artifact_root = artifact_workspace or workspace
    artifacts = _rebase_file_artifact_records(artifacts, workspace, artifact_root)
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
        'artifacts': _rows_to_wire(
            artifacts, artifact_root, skills_dir,
            default_profile=default_profile, collection='artifacts',
        ),
        'references': _rows_to_wire(
            references, workspace, skills_dir, collection='references',
        ),
    }
    if sequence is not None:
        payload['sequence'] = sequence
    if normalized_event.name == 'todo' and normalized_event.status == 'completed':
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
    default_profile: str = '',
    artifact_workspace: Path | None = None,
) -> dict[str, Any]:
    """Build a manifest_delta SSE payload from assistant MEDIA: tokens in one turn."""
    return extract_manifest_delta_from_turn_reconcile(
        messages,
        workspace,
        session_id=session_id,
        stream_id=stream_id,
        turn_key=turn_key,
        sequence=sequence,
        default_profile=default_profile,
        artifact_workspace=artifact_workspace,
    )


def extract_manifest_delta_from_turn_reconcile(
    messages: list,
    workspace: Path,
    *,
    session_id: str = '',
    stream_id: str = '',
    turn_key: str = '',
    sequence: int | None = None,
    default_profile: str = '',
    skills_dir: Path | None = None,
    tool_calls: list | None = None,
    artifact_workspace: Path | None = None,
) -> dict[str, Any]:
    """Build a turn_complete manifest_delta from transcript reconcile (incl. MEDIA)."""
    turn_key = str(turn_key or '').strip()
    if not turn_key:
        return {}
    session_artifacts, turn_artifacts = _reconcile_turn_artifact_rows(
        messages,
        workspace,
        turn_key,
        skills_dir=skills_dir,
        tool_calls=tool_calls,
    )
    artifact_root = artifact_workspace or workspace
    session_artifacts = _rebase_file_artifact_records(session_artifacts, workspace, artifact_root)
    turn_artifacts = _rebase_file_artifact_records(turn_artifacts, workspace, artifact_root)
    wire_artifacts = _rows_to_wire(
        session_artifacts, artifact_root, skills_dir,
        default_profile=default_profile, collection='artifacts',
    )
    if not wire_artifacts:
        return {}
    turn_wire = _rows_to_wire(
        turn_artifacts, artifact_root, skills_dir,
        default_profile=default_profile, collection='artifacts',
    )
    payload: dict[str, Any] = {
        'version': 1,
        'session_id': str(session_id or ''),
        'stream_id': str(stream_id or ''),
        'turn_key': turn_key,
        'source': {
            'kind': 'turn_complete',
            'tool': TURN_RECONCILE_SOURCE,
            'tid': '',
            'status': 'completed',
        },
        'artifacts': wire_artifacts,
        'turns': [{
            'turn_key': turn_key,
            'artifacts': turn_wire,
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
    messages = merge_session_messages_append_only(
        session.messages,
        state_db_messages,
        truncation_watermark=getattr(session, 'truncation_watermark', None),
    )
    if str(getattr(session, 'source_tag', '') or '').strip().lower() == 'cron':
        try:
            from integration.crons.hooks import normalize_cron_manifest_messages

            messages = normalize_cron_manifest_messages(
                messages,
                require_stable_real_turn=True,
            )
        except Exception:
            logger.debug("failed to normalize cron manifest messages", exc_info=True)
    return messages


def build_session_manifest(session, source_info: dict[str, str] | None = None) -> dict[str, Any]:
    """Build structured todos, artifacts, and references for one session."""
    from api.compression_anchor import is_context_compression_marker

    store_rows: list[dict[str, Any]] = []
    decided_turn_keys: set[str] = set()
    artifact_manifest_disabled = False
    if source_info is not None:
        # The HTTP endpoint must be store-authoritative.  In particular, an
        # older session without a decision must not regain artifacts merely by
        # opening the Inspector and reinterpreting its transcript/tool calls.
        source_info['manifest_source'] = 'unknown'
        workspace = Path(str(session.workspace)).expanduser().resolve()
        try:
            from api.session_manifest_store import (
                effective_manifest_workspace_root,
                load_manifest_decided_turn_keys,
                load_manifest_decided_turn_keys_by_root,
                load_manifest_records,
            )

            store_rows = load_manifest_records(session, include_lineage=True)
            current_root = effective_manifest_workspace_root(workspace)
            decided_turn_keys = {
                turn_key
                for turn_key, root in load_manifest_decided_turn_keys_by_root(session, include_lineage=True)
                if current_root is not None and root == str(current_root)
            }
            decided_turn_keys.update(load_manifest_decided_turn_keys(session, include_lineage=True))
        except Exception:
            logger.debug("failed to read session manifest store", exc_info=True)
            return {
                'todos': {'items': []},
                'artifacts': [],
                'references': [],
                'turns': [],
                'diagnostics': {
                    'missing_turn_key_message_indices': [],
                    'orphan_turn_keys': [],
                },
            }
        if not store_rows and not decided_turn_keys:
            source_info['manifest_source'] = 'none'
            # References are transcript-derived and do not use the artifact
            # store. Keep the artifact-store empty decision while still
            # exposing completed reference evidence for historical sessions.
            artifact_manifest_disabled = True
        else:
            source_info['manifest_source'] = 'db'
    messages = _load_display_messages(session)
    messages = _ensure_turn_keys(messages)
    has_stable_turn_keys = any(
        isinstance(message, dict)
        and message.get('role') == 'user'
        and not _is_synthetic_control_message(message)
        and str(message.get('_turn_key') or '').strip()
        for message in messages
    )
    missing_turn_key_message_indices = [
        idx for idx, message in enumerate(messages)
        if has_stable_turn_keys
        and isinstance(message, dict)
        and message.get('role') == 'user'
        and not is_context_compression_marker(message)
        and not _is_synthetic_control_message(message)
        and not str(message.get('_turn_key') or '').strip()
    ]
    tool_calls = list(getattr(session, 'tool_calls', None) or [])
    workspace = Path(str(session.workspace)).expanduser().resolve()
    artifact_workspace = artifact_workspace_root_for_session(session)
    skills_dir = _skills_dir_for_session(session)
    default_profile = str(getattr(session, 'profile', None) or '').strip()
    events = _collect_tool_events(messages, tool_calls)
    events.extend(_collect_media_artifact_events(messages, workspace))
    # Final assistant prose requires turn-ordered strong/prior path context and
    # is therefore added only by the reconcile pass below.
    todos = _extract_latest_todos(messages)
    artifacts, references, turns = _extract_manifest_records(
        events, workspace, messages, skills_dir=skills_dir,
    )

    if source_info is None:
        try:
            from api.session_manifest_store import (
                effective_manifest_workspace_root,
                load_manifest_decided_turn_keys,
                load_manifest_decided_turn_keys_by_root,
                load_manifest_records,
            )

            # Historical artifact backfill and empty-decision repair deliberately
            # remain disabled on manifest reads. GET must not mutate the artifact
            # store merely because a user opens an older session.
            # repair_empty_manifest_turns(session)
            store_rows = load_manifest_records(session, include_lineage=True)
            current_root = effective_manifest_workspace_root(artifact_workspace)
            decided_turn_keys = {
                turn_key
                for turn_key, root in load_manifest_decided_turn_keys_by_root(session, include_lineage=True)
                if current_root is not None and root == str(current_root)
            }
            # Keep the public current-root helper as the compatibility seam for
            # callers/tests that substitute the manifest-store reader.  Its real
            # implementation is also root-scoped, so this cannot reintroduce
            # cross-root suppression in production.
            decided_turn_keys.update(load_manifest_decided_turn_keys(session, include_lineage=True))
        except Exception:
            logger.debug("failed to read session manifest store", exc_info=True)

    artifact_records = {} if decided_turn_keys or artifact_manifest_disabled else _records_by_path(artifacts)
    if decided_turn_keys or artifact_manifest_disabled:
        for turn in turns:
            turn['artifacts'] = []
    else:
        _apply_turn_reconcile_to_manifest_records(
            artifact_records,
            turns,
            messages,
            workspace,
            skills_dir=skills_dir,
            tool_calls=tool_calls,
            skip_turn_keys=decided_turn_keys,
        )

    # Prefer persisted turn_artifacts over reconcile results.
    # When the streaming pipeline persists artifact paths at turn completion,
    # those paths are more reliable than the reconcile pass (which can suffer
    # from cross-turn prose contamination — see docs/architecture/turn-key-backend.md §8.3).
    persisted = getattr(session, 'turn_artifacts', None)
    if not decided_turn_keys and not artifact_manifest_disabled and isinstance(persisted, dict) and persisted:
        default_profile = str(getattr(session, 'profile', None) or '').strip()
        for turn in turns:
            tk = turn.get('turn_key', '')
            if str(tk or '').strip() in decided_turn_keys:
                continue
            if tk not in persisted:
                continue
            entries = persisted[tk]
            if not isinstance(entries, list):
                continue
            _merge_persisted_turn_artifact_records(
                turn,
                entries,
                default_profile=default_profile,
                artifact_records=artifact_records,
                skills_dir=skills_dir,
            )

    turn_rows_by_key: dict[str, dict[str, Any]] = {
        str(turn.get('turn_key') or ''): turn for turn in turns
    }
    profiled_artifact_records: dict[str, dict[str, Any]] = {
        _artifact_identity(row, default_profile): row for row in artifact_records.values()
    }
    orphan_turn_keys: set[str] = set()
    for store_row in store_rows:
        record = _store_artifact_record(store_row)
        if record is None:
            continue
        if _is_skill_manifest_row(record):
            canonical = _canonical_skill_manifest_path(str(record.get('path') or ''), skills_dir)
            if canonical:
                record['path'] = canonical
                record['skill_name'] = canonical
        identity = _artifact_identity(record, default_profile)
        profiled_artifact_records[identity] = record
        tk = str(record.get('turn_key') or '').strip()
        if not tk:
            continue
        turn = turn_rows_by_key.get(tk)
        if turn is None:
            orphan_turn_keys.add(tk)
            continue
        turn_records = {
            _artifact_identity(row, default_profile): row
            for row in list(turn.get('artifacts') or [])
            if isinstance(row, dict)
        }
        turn_records[identity] = record
        turn['artifacts'] = sorted(
            turn_records.values(),
            key=lambda row: (str(row.get('profile') or ''), str(row.get('path') or '')),
        )

    artifacts = sorted(
        _rebase_file_artifact_records(
            list(profiled_artifact_records.values()), workspace, artifact_workspace,
        ),
        key=lambda row: (str(row.get('profile') or ''), str(row.get('path') or '')),
    )
    for turn in turns:
        turn['artifacts'] = _rebase_file_artifact_records(
            list(turn.get('artifacts') or []), workspace, artifact_workspace,
        )
    _drop_reference_skills_in_artifacts(artifacts, references, turns, skills_dir)
    if artifact_manifest_disabled:
        artifacts = []
        for turn in turns:
            turn['artifacts'] = []
    _clean_record_keys(artifacts + references)
    return {
        'todos': _wire_todos(todos),
        'artifacts': _rows_to_wire(
            artifacts, artifact_workspace, skills_dir,
            default_profile=default_profile, collection='artifacts',
        ),
        'references': _rows_to_wire(
            references, workspace, skills_dir, collection='references',
        ),
        'turns': [
            _turn_to_wire(turn, artifact_workspace, skills_dir, default_profile=default_profile)
            for turn in turns
        ],
        'diagnostics': {
            'missing_turn_key_message_indices': missing_turn_key_message_indices,
            'orphan_turn_keys': sorted(orphan_turn_keys),
        },
    }

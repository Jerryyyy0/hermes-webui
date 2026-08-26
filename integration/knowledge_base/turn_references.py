"""Normalize known knowledge-base MCP results into manifest reference rows."""

from __future__ import annotations

import json
import math
import re
from pathlib import PurePath
from typing import Any

SINGLE_SEARCH_TOOL = "mcp__ithink_kb_mcp__searchKnowledgeBaseDocuments"
ACROSS_SEARCH_TOOL = "mcp__ithink_kb_mcp__searchKnowledgeBaseDocumentsAcross"
_SUPPORTED_TOOLS = {
    SINGLE_SEARCH_TOOL.lower(): SINGLE_SEARCH_TOOL,
    ACROSS_SEARCH_TOOL.lower(): ACROSS_SEARCH_TOOL,
}
_MAX_RESULT_BYTES = 1024 * 1024
_MAX_RESULT_ROWS = 50
_UNTRUSTED_RESULT_OPEN = "<untrusted_tool_result"
_UNTRUSTED_RESULT_CLOSE = "</untrusted_tool_result>"
_WRAPPED_RESULT_START = re.compile(r'\{\s*"result"\s*:')


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _score(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return value if math.isfinite(value) else None
    except (OverflowError, TypeError):
        return None


def _chunk(page_content: str, score: Any) -> dict[str, Any]:
    chunk: dict[str, Any] = {"page_content": page_content}
    normalized_score = _score(score)
    chunk["score"] = normalized_score if normalized_score is not None else ""
    return chunk


def _canonical_tool_name(name: Any) -> str:
    return _SUPPORTED_TOOLS.get(_text(name).lower(), "")


def _json(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _untrusted_tool_result_json(value: Any) -> dict[str, Any] | None:
    """Extract one preserved JSON result from the Agent's safety wrapper."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text.startswith(_UNTRUSTED_RESULT_OPEN) or not text.endswith(_UNTRUSTED_RESULT_CLOSE):
        return None
    header_end = text.find(">")
    body_end = text.rfind(_UNTRUSTED_RESULT_CLOSE)
    if header_end < 0 or body_end <= header_end:
        return None
    body = text[header_end + 1:body_end]
    decoder = json.JSONDecoder()
    candidates: list[dict[str, Any]] = []
    for match in _WRAPPED_RESULT_START.finditer(body):
        try:
            parsed, _end = decoder.raw_decode(body[match.start():])
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(parsed, dict) and "result" in parsed:
            candidates.append(parsed)
    return candidates[0] if len(candidates) == 1 else None


def _result_rows(result: Any) -> list[dict[str, Any]]:
    if isinstance(result, str) and len(result.encode("utf-8")) > _MAX_RESULT_BYTES:
        return []
    outer = _json(result)
    if outer is None:
        outer = _untrusted_tool_result_json(result)
    if isinstance(outer, dict) and "result" in outer:
        outer = _json(outer.get("result"))
    if not isinstance(outer, list) or len(outer) > _MAX_RESULT_ROWS:
        return []
    return [row for row in outer if isinstance(row, dict)]


def _source_file_name(raw_source: Any) -> str:
    source = _text(raw_source).replace("\\", "/")
    if not source or "\x00" in source:
        return ""
    name = PurePath(source).name.strip()
    return "" if name in ("", ".", "..") else name


def reference_key(row: dict[str, Any]) -> str:
    return "\0".join((
        _text(row.get("kb_name")),
        _text(row.get("file_name")),
    ))


def extract_references(
    *,
    name: Any,
    args: Any,
    result: Any,
    status: Any = "completed",
    tid: Any = "",
) -> list[dict[str, Any]]:
    """Return untrusted MCP results as minimal internal reference candidates.

    The caller owns tool-event success validation. This parser still requires a
    completed event and only recognizes the two exact MCP tool names.
    """
    tool = _canonical_tool_name(name)
    if not tool or _text(status).lower() != "completed":
        return []
    args = args if isinstance(args, dict) else {}
    candidates: list[dict[str, Any]] = []
    for row in _result_rows(result):
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        content = row.get("page_content")
        if not isinstance(content, str) or not content:
            continue
        if tool == SINGLE_SEARCH_TOOL:
            kb_name = _text(args.get("kbName"))
            file_name = _source_file_name(metadata.get("source"))
        else:
            kb_name = _text(metadata.get("kbName"))
            file_name = _text(metadata.get("fileName"))
        if not kb_name or not file_name:
            continue
        candidates.append({
            "kind": "knowledge_base_document",
            "resource_type": "knowledge_base_document",
            "kb_name": kb_name,
            "file_name": file_name,
            "chunks": [_chunk(content, row.get("score"))],
            "sources": [{"tool": tool, "tid": _text(tid)}],
        })
    return merge_reference_rows([], candidates)


def merge_reference_rows(
    existing: list[dict[str, Any]], incoming: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge same-document rows, preserving source and passage encounter order."""
    rows: dict[str, dict[str, Any]] = {}
    for row in [*existing, *incoming]:
        if not isinstance(row, dict):
            continue
        key = reference_key(row)
        if not key or key == "\0":
            continue
        current = rows.get(key)
        if current is None:
            current = {
                "kind": "knowledge_base_document",
                "resource_type": "knowledge_base_document",
                "kb_name": _text(row.get("kb_name")),
                "file_name": _text(row.get("file_name")),
                "chunks": [],
                "sources": [],
            }
            rows[key] = current
        source_keys = {
            (_text(source.get("tool")), _text(source.get("tid")))
            for source in current["sources"]
            if isinstance(source, dict)
        }
        for source in row.get("sources") or []:
            if not isinstance(source, dict):
                continue
            normalized = {"tool": _text(source.get("tool")), "tid": _text(source.get("tid"))}
            if not normalized["tool"]:
                continue
            source_key = (normalized["tool"], normalized["tid"])
            if source_key not in source_keys:
                current["sources"].append(normalized)
                source_keys.add(source_key)
        chunks_by_content = {
            chunk.get("page_content"): chunk
            for chunk in current["chunks"]
            if isinstance(chunk, dict) and isinstance(chunk.get("page_content"), str)
        }
        for chunk in row.get("chunks") or []:
            if not isinstance(chunk, dict):
                continue
            page_content = chunk.get("page_content")
            if not isinstance(page_content, str) or not page_content:
                continue
            normalized = _chunk(page_content, chunk.get("score"))
            existing_chunk = chunks_by_content.get(page_content)
            if existing_chunk is None:
                current["chunks"].append(normalized)
                chunks_by_content[page_content] = normalized
            elif "score" not in existing_chunk and "score" in normalized:
                existing_chunk["score"] = normalized["score"]
    return sorted(rows.values(), key=lambda row: (row["kb_name"], row["file_name"]))


def to_wire(row: dict[str, Any]) -> dict[str, Any] | None:
    """Project an internal knowledge-base row onto the public manifest wire."""
    merged = merge_reference_rows([], [row])
    if len(merged) != 1:
        return None
    record = merged[0]
    if not record["sources"] or not record["chunks"]:
        return None
    return {
        "kind": "knowledge_base_document",
        "source": record["sources"],
        "metadata": {
            "kbName": record["kb_name"],
            "fileName": record["file_name"],
            "chunks": record["chunks"],
        },
    }

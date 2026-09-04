"""Normalize known knowledge-base MCP results into manifest reference rows."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
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


def _index(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return parsed if parsed >= 0 else 0


def _base64url_digest(value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _immutable_document_id(metadata: dict[str, Any]) -> str:
    for key in ("document_id", "documentId"):
        value = _text(metadata.get(key))
        if value:
            return value
    return ""


def _immutable_chunk_id(raw_chunk: dict[str, Any]) -> str:
    for key in ("chunk_id", "chunkId", "id"):
        value = _text(raw_chunk.get(key))
        if value:
            return value
    return ""


def _document_id(
    *,
    metadata: dict[str, Any],
    kb_name: str,
    file_name: str,
    profile: str,
    session_id: str,
    authority_namespace: str,
) -> str:
    immutable_id = _immutable_document_id(metadata)
    namespace = _text(authority_namespace)
    if immutable_id and namespace:
        key = "immutable\0" + namespace + "\0" + immutable_id
    elif namespace:
        key = "named\0" + namespace + "\0" + kb_name + "\0" + file_name
    else:
        local_identity = immutable_id or (kb_name + "\0" + file_name)
        key = "session\0" + _text(profile) + "\0" + _text(session_id) + "\0" + local_identity
    return "kbdoc:v1:" + _base64url_digest(key)


def _chunk_id(
    *,
    raw_chunk: dict[str, Any],
    reference_id: str,
    tool: str,
    tid: str,
    row_index: int,
    chunk_index: int,
    page_content: str,
) -> tuple[str, bool]:
    immutable_id = _immutable_chunk_id(raw_chunk)
    if immutable_id:
        key = "immutable\0" + reference_id + "\0" + immutable_id
        return "kbchunk:v1:" + _base64url_digest(key), True
    source_call_key = tool + "\0" + tid
    key = (
        "call-scoped\0" + source_call_key + "\0" + str(row_index)
        + "\0" + str(chunk_index) + "\0" + page_content
    )
    return "kbchunk:v1:" + _base64url_digest(key), False


def _chunk(
    page_content: str,
    score: Any,
    *,
    chunk_id: str,
    immutable: bool,
    source_tool: str,
    source_tid: str,
    row_index: int = 0,
    chunk_index: int = 0,
) -> dict[str, Any]:
    chunk: dict[str, Any] = {"page_content": page_content}
    normalized_score = _score(score)
    chunk["score"] = normalized_score if normalized_score is not None else ""
    chunk["chunk_id"] = chunk_id
    chunk["_immutable_chunk_id"] = immutable
    chunk["_source_tool"] = source_tool
    chunk["_source_tid"] = source_tid
    chunk["_row_index"] = row_index
    chunk["_chunk_index"] = chunk_index
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


def reference_key(row: dict[str, Any]) -> str:
    reference_id = _text(row.get("reference_id"))
    if reference_id:
        return reference_id
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
    profile: Any = "",
    session_id: Any = "",
    authority_namespace: Any = "",
) -> list[dict[str, Any]]:
    """Return grouped MCP document results as internal reference candidates.

    Both supported tools return the same document-grouped structure. A top-level
    row identifies one document through ``metadata.kbName`` and
    ``metadata.fileName``; its original, independently-scored passages are in
    ``chunks``. Do not infer identity from request arguments or legacy result
    fields: references are a strict transcript-derived index.
    """
    tool = _canonical_tool_name(name)
    if not tool or _text(status).lower() != "completed":
        return []
    candidates: list[dict[str, Any]] = []
    normalized_tid = _text(tid)
    for row_index, row in enumerate(_result_rows(result)):
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        if row.get("type") != "Document":
            continue
        kb_name = _text(metadata.get("kbName"))
        file_name = _text(metadata.get("fileName"))
        if not kb_name or not file_name:
            continue
        reference_id = _document_id(
            metadata=metadata,
            kb_name=kb_name,
            file_name=file_name,
            profile=_text(profile),
            session_id=_text(session_id),
            authority_namespace=_text(authority_namespace),
        )
        chunks = []
        for chunk_index, raw_chunk in enumerate(
            row.get("chunks") if isinstance(row.get("chunks"), list) else []
        ):
            if not isinstance(raw_chunk, dict):
                continue
            content = raw_chunk.get("page_content")
            if not isinstance(content, str) or not content:
                continue
            chunk_id, immutable = _chunk_id(
                raw_chunk=raw_chunk,
                reference_id=reference_id,
                tool=tool,
                tid=normalized_tid,
                row_index=row_index,
                chunk_index=chunk_index,
                page_content=content,
            )
            chunks.append(_chunk(
                content,
                raw_chunk.get("score"),
                chunk_id=chunk_id,
                immutable=immutable,
                source_tool=tool,
                source_tid=normalized_tid,
                row_index=row_index,
                chunk_index=chunk_index,
            ))
        if not chunks:
            continue
        candidates.append({
            "kind": "knowledge_base_document",
            "resource_type": "knowledge_base_document",
            "kb_name": kb_name,
            "file_name": file_name,
            "reference_id": reference_id,
            "chunks": chunks,
            "sources": [{"tool": tool, "tid": normalized_tid}],
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
            kb_name = _text(row.get("kb_name"))
            file_name = _text(row.get("file_name"))
            current = {
                "kind": "knowledge_base_document",
                "resource_type": "knowledge_base_document",
                "kb_name": kb_name,
                "file_name": file_name,
                "reference_id": _text(row.get("reference_id")) or _document_id(
                    metadata={},
                    kb_name=kb_name,
                    file_name=file_name,
                    profile="",
                    session_id="",
                    authority_namespace="",
                ),
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
        chunks_by_identity = {
            chunk.get("chunk_id"): chunk
            for chunk in current["chunks"]
            if isinstance(chunk, dict) and chunk.get("chunk_id")
        }
        for chunk in row.get("chunks") or []:
            if not isinstance(chunk, dict):
                continue
            page_content = chunk.get("page_content")
            if not isinstance(page_content, str) or not page_content:
                continue
            chunk_id = _text(chunk.get("chunk_id"))
            if not chunk_id:
                # Legacy rows without IDs remain distinct by encounter order;
                # never collapse them by their text.
                chunk_id = "kbchunk:v1:" + _base64url_digest(
                    "legacy\0" + str(len(current["chunks"])) + "\0" + page_content
                )
            normalized = _chunk(
                page_content,
                chunk.get("score"),
                chunk_id=chunk_id,
                immutable=bool(chunk.get("_immutable_chunk_id")),
                source_tool=_text(chunk.get("_source_tool")),
                source_tid=_text(chunk.get("_source_tid")),
                row_index=_index(chunk.get("_row_index", 0)),
                chunk_index=_index(chunk.get("_chunk_index", 0)),
            )
            existing_chunk = chunks_by_identity.get(chunk_id)
            if existing_chunk is None:
                current["chunks"].append(normalized)
                chunks_by_identity[chunk_id] = normalized
            elif existing_chunk.get("score") == "" and normalized.get("score") != "":
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
        "id": record.get("reference_id"),
        "source": record["sources"],
        "metadata": {
            "kbName": record["kb_name"],
            "fileName": record["file_name"],
            "chunks": [
                {
                    "id": chunk.get("chunk_id"),
                    "page_content": chunk.get("page_content", ""),
                    "score": chunk.get("score", ""),
                }
                for chunk in record["chunks"]
            ],
        },
    }

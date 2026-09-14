"""Host-owned citation candidates and final assistant settlement helpers."""

from __future__ import annotations

import copy
import json
import re
import secrets
import threading
from dataclasses import dataclass
from typing import Any

from integration.knowledge_base.turn_references import (
    ACROSS_SEARCH_TOOL,
    SINGLE_SEARCH_TOOL,
    extract_references,
    sort_chunks_by_score,
)

_TOOLS = frozenset({SINGLE_SEARCH_TOOL, ACROSS_SEARCH_TOOL})
_TOKEN_RE = re.compile(r"\[\[c:([A-Za-z0-9_-]{16})\]\]")
_PUBLIC_MARKER_RE = re.compile(r'<sup data-c="([1-9][0-9]*)">\[([1-9][0-9]*)\]</sup>')
_WRAPPER_RE = re.compile(r"^(<untrusted_tool_result\b[^>]*>)(.*)(</untrusted_tool_result>)$", re.S)


def _is_internal_marker_prefix(value: str) -> bool:
    if value in {"[", "[[", "[[c", "[[c:"}:
        return True
    if not value.startswith("[[c:"):
        return False
    tail = value[4:]
    token = tail.rstrip("]")
    closing = tail[len(token):]
    return (
        len(token) <= 16
        and all(ch.isalnum() or ch in "_-" for ch in token)
        and closing in {"", "]", "]]"}
        and (closing != "]]" or len(token) == 16)
    )


class CitationTokenStreamFilter:
    """Suppress internal citation markers even when split across SSE deltas."""

    def __init__(self) -> None:
        self._buffer = ""

    def feed(self, value: Any) -> str:
        self._buffer += str(value or "")
        visible: list[str] = []
        while self._buffer:
            match = _TOKEN_RE.search(self._buffer)
            if match is not None:
                visible.append(self._buffer[:match.start()])
                self._buffer = self._buffer[match.end():]
                continue
            pending_at = None
            for index in range(max(0, len(self._buffer) - 22), len(self._buffer)):
                if _is_internal_marker_prefix(self._buffer[index:]):
                    pending_at = index
                    break
            if pending_at is None:
                visible.append(self._buffer)
                self._buffer = ""
            else:
                visible.append(self._buffer[:pending_at])
                self._buffer = self._buffer[pending_at:]
            break
        return "".join(visible)

    def finish(self) -> str:
        pending = self._buffer
        self._buffer = ""
        # A truncated marker can end the stream before its closing ``]]``.
        # Remove the reserved syntax as well as complete markers so it cannot
        # leak through cancel/error snapshots.
        pending = _TOKEN_RE.sub("", pending)
        return re.sub(r"\[\[c:[A-Za-z0-9_-]{0,16}\]?", "", pending)


def strip_internal_citation_markers(value: Any) -> Any:
    """Remove reserved Citation syntax from a complete outbound value."""
    if isinstance(value, str):
        stream_filter = CitationTokenStreamFilter()
        return stream_filter.feed(value) + stream_filter.finish()
    if isinstance(value, list):
        return [strip_internal_citation_markers(item) for item in value]
    if isinstance(value, dict):
        return {key: strip_internal_citation_markers(item) for key, item in value.items()}
    return value


def _sort_adjacent_public_markers(value: str) -> str:
    """Canonicalize one claim's consecutive Citation markers by ordinal.

    Marker identity remains unchanged: only adjacent markers, optionally
    separated by spaces or tabs, are reordered.  Newlines and ordinary prose
    always terminate a group, so citations from different claims are never
    moved across their text boundary.
    """
    matches = [
        match for match in _PUBLIC_MARKER_RE.finditer(value)
        if match.group(1) == match.group(2)
    ]
    if len(matches) < 2:
        return value

    pieces: list[str] = []
    cursor = 0
    group: list[re.Match[str]] = [matches[0]]

    def append_group(markers: list[re.Match[str]]) -> None:
        nonlocal cursor
        first, last = markers[0], markers[-1]
        pieces.append(value[cursor:first.start()])
        if len(markers) == 1:
            pieces.append(first.group(0))
        else:
            pieces.extend(
                marker.group(0)
                for marker in sorted(markers, key=lambda marker: int(marker.group(1)))
            )
        cursor = last.end()

    for marker in matches[1:]:
        between = value[group[-1].end():marker.start()]
        if re.fullmatch(r"[ \t]*", between):
            group.append(marker)
            continue
        append_group(group)
        group = [marker]
    append_group(group)
    pieces.append(value[cursor:])
    return "".join(pieces)


@dataclass(frozen=True)
class CandidateScope:
    profile: str
    session_id: str
    stream_id: str
    worker_generation: int | str
    turn_key: str


@dataclass(frozen=True)
class CitationCandidate:
    token: str
    scope: CandidateScope
    tool: str
    tid: str
    row_index: int
    chunk_index: int
    reference_id: str
    chunk_id: str
    evidence: dict[str, Any]


@dataclass(frozen=True)
class CitationSettlement:
    settlement_id: str
    scope: CandidateScope
    public_content: str
    citations: tuple[dict[str, Any], ...]
    evidence: tuple[dict[str, Any], ...]
    tokens: frozenset[str]


def _valid_tid(value: Any) -> str:
    tid = str(value or "").strip()
    if not tid or len(tid) > 256 or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in tid):
        return ""
    if not re.fullmatch(r"[A-Za-z0-9_.:-]+", tid):
        return ""
    return tid


class CandidateRegistry:
    """Per-stream registry; no candidate is valid outside its frozen scope."""

    def __init__(self, scope: CandidateScope):
        self.scope = scope
        self._lock = threading.RLock()
        self._by_token: dict[str, CitationCandidate] = {}
        self._by_position: dict[tuple[str, str, int, int], CitationCandidate] = {}
        self._reserved: dict[str, str] = {}

    def add_completed_result(self, *, name: Any, args: Any, result: Any, tid: Any, status: Any = "completed") -> list[CitationCandidate]:
        tool = str(name or "").strip()
        normalized_tid = _valid_tid(tid)
        if tool not in _TOOLS or not normalized_tid or str(status or "").lower() != "completed":
            return []
        rows = extract_references(
            name=tool,
            args=args,
            result=result,
            status=status,
            tid=normalized_tid,
            profile=self.scope.profile,
            session_id=self.scope.session_id,
        )
        created: list[CitationCandidate] = []
        with self._lock:
            for row in rows:
                reference_id = str(row.get("reference_id") or "")
                for chunk in row.get("chunks") or []:
                    if not isinstance(chunk, dict):
                        continue
                    chunk_id = str(chunk.get("chunk_id") or "")
                    row_index = int(chunk.get("_row_index", 0))
                    chunk_index = int(chunk.get("_chunk_index", 0))
                    if not reference_id or not chunk_id:
                        continue
                    position = (tool, normalized_tid, row_index, chunk_index)
                    existing = self._by_position.get(position)
                    if existing is not None and existing.reference_id == reference_id and existing.chunk_id == chunk_id:
                        created.append(existing)
                        continue
                    token = secrets.token_urlsafe(12)
                    candidate = CitationCandidate(
                        token=token,
                        scope=self.scope,
                        tool=tool,
                        tid=normalized_tid,
                        row_index=row_index,
                        chunk_index=chunk_index,
                        reference_id=reference_id,
                        chunk_id=chunk_id,
                        evidence={
                            "reference_id": reference_id,
                            "chunk_id": chunk_id,
                            "kb_name": str(row.get("kb_name") or ""),
                            "file_name": str(row.get("file_name") or ""),
                            "page_content": chunk.get("page_content", ""),
                            "score": chunk.get("score", ""),
                        },
                    )
                    self._by_token[token] = candidate
                    self._by_position[position] = candidate
                    created.append(candidate)
        return created

    def token_for(self, *, tool: Any, tid: Any, row_index: int, chunk_index: int) -> str | None:
        key = (str(tool or "").strip(), _valid_tid(tid), int(row_index), int(chunk_index))
        with self._lock:
            candidate = self._by_position.get(key)
            return candidate.token if candidate else None

    def candidate_for_token(self, token: str) -> CitationCandidate | None:
        with self._lock:
            return self._by_token.get(token)

    def reserve(self, tokens: list[str], settlement_id: str) -> bool:
        with self._lock:
            if not tokens or any(token not in self._by_token or token in self._reserved for token in tokens):
                return False
            for token in tokens:
                self._reserved[token] = settlement_id
            return True

    def release(self, settlement_id: str) -> None:
        with self._lock:
            for token, owner in list(self._reserved.items()):
                if owner == settlement_id:
                    self._reserved.pop(token, None)

    def consume(self, settlement_id: str) -> None:
        """Consume tokens after the host session save has committed."""
        with self._lock:
            tokens = [token for token, owner in self._reserved.items() if owner == settlement_id]
            for token in tokens:
                candidate = self._by_token.pop(token, None)
                self._reserved.pop(token, None)
                if candidate is not None:
                    self._by_position.pop(
                        (candidate.tool, candidate.tid, candidate.row_index, candidate.chunk_index),
                        None,
                    )

    def clear(self) -> None:
        with self._lock:
            self._by_token.clear()
            self._by_position.clear()
            self._reserved.clear()


class KnowledgeBaseCitationHook:
    """Adapter passed to Agent runtimes that implement the citation seam."""

    def __init__(self, registry: CandidateRegistry):
        self.registry = registry
        self.scope = registry.scope
        self._settlements: dict[str, CitationSettlement] = {}

    def annotate_tool_content_for_provider(self, *, tool_call_id: Any, tool_name: Any, content: Any) -> Any:
        return annotate_tool_content_for_provider(
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            content=content,
            registry=self.registry,
        )

    def provider_system_prompt(self) -> str:
        return (
            "Knowledge-base citation rule:\n"
            "A knowledge-base chunk is one object inside a knowledge-base tool "
            "result's `chunks` array. Its `page_content` is the factual evidence, "
            "the same chunk's `_cite` is the citation token bound to that evidence, "
            "and `score` is retrieval metadata rather than a citation token.\n"
            "When the final answer makes a factual claim supported by a chunk's "
            "`page_content`, append that same chunk's exact `_cite` value immediately "
            "after the supported claim. Build the marker from the literal prefix "
            "`[[c:`, the exact `_cite` value, and the literal suffix `]]`.\n"
            "Format-only example: if a supporting chunk has "
            "`page_content: Refunds close after seven days` and "
            "`_cite: K7x8pQm2Vt4zAa9B`, write "
            "`Refunds close after seven days.[[c:K7x8pQm2Vt4zAa9B]]`. "
            "The example token is not evidence; never reuse this example token.\n"
            "If a claim is not supported by an annotated chunk, write it without a "
            "citation marker. Do not cite a chunk merely because it appeared in search results.\n"
            "Use only `_cite` values present in the provided chunks. Never use a token "
            "from a different chunk, and do not invent, modify, translate, renumber, "
            "or expose citation tokens in any other form."
        )

    def prepare_final_assistant(self, *, raw_content: Any) -> dict[str, Any]:
        settlement = settle_final_assistant(
            raw_content=raw_content,
            scope=self.scope,
            registry=self.registry,
        )
        if not settlement.citations:
            return {"content": settlement.public_content, "settlement_id": ""}
        self._settlements[settlement.settlement_id] = settlement
        return {
            "content": settlement.public_content,
            "settlement_id": settlement.settlement_id,
        }

    def settlement(self, settlement_id: Any) -> CitationSettlement | None:
        return self._settlements.get(str(settlement_id or ""))

    def invalidate(self, settlement_id: Any, *, reason: str = "host_attach_or_save_failed") -> None:
        settlement = self._settlements.pop(str(settlement_id or ""), None)
        if settlement:
            self.registry.release(settlement.settlement_id)

    def invalidate_all(self, *, reason: str = "stream_terminal") -> None:
        for settlement_id in list(self._settlements):
            self.invalidate(settlement_id, reason=reason)

    def mark_attached_pending(self, settlement_id: Any) -> bool:
        settlement = self.settlement(settlement_id)
        return settlement is not None

    def commit(self, settlement_id: Any) -> bool:
        settlement = self._settlements.pop(str(settlement_id or ""), None)
        if settlement is None:
            return False
        self.registry.consume(settlement.settlement_id)
        return True


def _annotate_rows(rows: Any, *, tool: str, tid: str, registry: CandidateRegistry) -> Any:
    if not isinstance(rows, list):
        return rows
    result = copy.deepcopy(rows)
    for row_index, row in enumerate(result):
        if not isinstance(row, dict) or not isinstance(row.get("chunks"), list):
            continue
        for chunk_index, chunk in enumerate(row["chunks"]):
            if not isinstance(chunk, dict):
                continue
            token = registry.token_for(
                tool=tool,
                tid=tid,
                row_index=row_index,
                chunk_index=chunk_index,
            )
            if token:
                chunk["_cite"] = token
        row["chunks"] = sort_chunks_by_score(row["chunks"])
    return result


def _annotate_payload(payload: Any, *, tool: str, tid: str, registry: CandidateRegistry) -> Any:
    if isinstance(payload, dict) and "result" in payload:
        raw_result = payload.get("result")
        parsed_result = raw_result
        encoded = isinstance(raw_result, str)
        if encoded:
            try:
                parsed_result = json.loads(raw_result)
            except (TypeError, ValueError, json.JSONDecodeError):
                return payload
        annotated = _annotate_rows(parsed_result, tool=tool, tid=tid, registry=registry)
        payload = copy.deepcopy(payload)
        payload["result"] = json.dumps(annotated, ensure_ascii=False) if encoded else annotated
        return payload
    return _annotate_rows(payload, tool=tool, tid=tid, registry=registry)


def annotate_tool_content_for_provider(*, tool_call_id: Any, tool_name: Any, content: Any, registry: CandidateRegistry) -> Any:
    """Annotate only the provider-facing copy of one tool message."""
    tool = str(tool_name or "").strip()
    tid = _valid_tid(tool_call_id)
    if tool not in _TOOLS or not tid:
        return content
    if isinstance(content, (dict, list)):
        return _annotate_payload(content, tool=tool, tid=tid, registry=registry)
    if not isinstance(content, str):
        return content
    wrapper = _WRAPPER_RE.match(content.strip())
    if wrapper:
        prefix, body, suffix = wrapper.groups()
        decoder = json.JSONDecoder()
        for match in re.finditer(r'\{\s*"result"\s*:', body):
            try:
                payload, end = decoder.raw_decode(body[match.start():])
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict):
                rewritten = _annotate_payload(payload, tool=tool, tid=tid, registry=registry)
                return prefix + body[:match.start()] + json.dumps(rewritten, ensure_ascii=False) + body[match.start() + end:] + suffix
        return content
    try:
        parsed = json.loads(content)
    except (TypeError, ValueError, json.JSONDecodeError):
        return content
    return json.dumps(_annotate_payload(parsed, tool=tool, tid=tid, registry=registry), ensure_ascii=False)


def settle_final_assistant(*, raw_content: Any, scope: CandidateScope, registry: CandidateRegistry) -> CitationSettlement:
    raw = str(raw_content or "")
    matches = list(_TOKEN_RE.finditer(raw))
    valid: list[tuple[re.Match[str], CitationCandidate]] = []
    for match in matches:
        candidate = registry.candidate_for_token(match.group(1))
        if candidate is not None and candidate.scope == scope:
            valid.append((match, candidate))
    settlement_id = secrets.token_urlsafe(24)
    tokens = [candidate.token for _, candidate in valid]
    if tokens and not registry.reserve(tokens, settlement_id):
        valid = []
        tokens = []
    pieces: list[str] = []
    citations: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    cursor = 0
    ordinal = 0
    # A Citation represents a cited chunk, rather than one textual occurrence
    # of its token.  A model can use the same evidence for several claims; all
    # those markers must point at the one Citation record and ordinal.
    ordinal_by_chunk: dict[tuple[str, str], int] = {}
    for match, candidate in valid:
        pieces.append(raw[cursor:match.start()])
        chunk_key = (candidate.reference_id, candidate.chunk_id)
        marker_ordinal = ordinal_by_chunk.get(chunk_key)
        if marker_ordinal is None:
            ordinal += 1
            marker_ordinal = ordinal
            ordinal_by_chunk[chunk_key] = marker_ordinal
            citations.append({
                "citation_id": f"kbcite:v1:{settlement_id}:{marker_ordinal}",
                "ordinal": marker_ordinal,
                "reference_id": candidate.reference_id,
                "chunk_ids": [candidate.chunk_id],
                "source": {"tool": candidate.tool, "tid": candidate.tid},
            })
            evidence.append(candidate.evidence)
        pieces.append(f'<sup data-c="{marker_ordinal}">[{marker_ordinal}]</sup>')
        cursor = match.end()
    pieces.append(raw[cursor:])
    public_content = "".join(pieces)
    public_content = _TOKEN_RE.sub("", public_content)
    public_content = _sort_adjacent_public_markers(public_content)
    return CitationSettlement(
        settlement_id=settlement_id,
        scope=scope,
        public_content=public_content,
        citations=tuple(citations),
        evidence=tuple(evidence),
        tokens=frozenset(tokens),
    )


def attach_settlement_to_message(message: dict[str, Any], settlement: CitationSettlement) -> bool:
    """Attach host-owned metadata to one already-selected final message."""
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return False
    if str(message.get("content") or "") != settlement.public_content:
        return False
    message["content"] = settlement.public_content
    if settlement.citations:
        message["citations"] = copy.deepcopy(list(settlement.citations))
        message["_knowledge_base_citation_evidence"] = copy.deepcopy(list(settlement.evidence))
    return True


def strip_orphan_citation_markers(value: Any) -> Any:
    """Remove rendered citation superscripts that have no committed mapping."""
    marker_re = re.compile(r"\s*<sup\s+data-c=\"\d+\">\[\d+\]</sup>")
    if isinstance(value, str):
        return marker_re.sub("", value)
    if isinstance(value, list):
        return [strip_orphan_citation_markers(item) for item in value]
    if isinstance(value, dict):
        result = dict(value)
        if "content" in result:
            result["content"] = strip_orphan_citation_markers(result["content"])
        return result
    return value


def extract_committed_reference_rows(messages: Any) -> list[tuple[int, dict[str, Any]]]:
    """Return only KB chunks selected by a committed assistant citation.

    Tool results are candidates, not proof of use. A row is accepted only when
    the same persisted assistant message contains a matching public marker,
    citation mapping and private evidence snapshot.
    """
    rows: list[tuple[int, dict[str, Any]]] = []
    for message_index, message in enumerate(messages or []):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        content = message.get("content")
        citations = message.get("citations")
        evidence = message.get("_knowledge_base_citation_evidence")
        if not isinstance(content, str) or not isinstance(citations, list) or not isinstance(evidence, list):
            continue
        evidence_by_key = {
            (str(item.get("reference_id") or ""), str(item.get("chunk_id") or "")): item
            for item in evidence
            if isinstance(item, dict)
        }
        seen_ordinals: set[int] = set()
        for citation in citations:
            if not isinstance(citation, dict):
                continue
            ordinal = citation.get("ordinal")
            if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal <= 0 or ordinal in seen_ordinals:
                continue
            marker = f'<sup data-c="{ordinal}">[{ordinal}]</sup>'
            if marker not in content:
                continue
            reference_id = str(citation.get("reference_id") or "")
            chunk_ids = citation.get("chunk_ids")
            if not isinstance(chunk_ids, list) or len(chunk_ids) != 1:
                continue
            chunk_id = str(chunk_ids[0] or "")
            item = evidence_by_key.get((reference_id, chunk_id))
            source = citation.get("source")
            if not item or not isinstance(source, dict):
                continue
            tool = str(source.get("tool") or "")
            tid = _valid_tid(source.get("tid"))
            kb_name = str(item.get("kb_name") or "").strip()
            file_name = str(item.get("file_name") or "").strip()
            page_content = item.get("page_content")
            if tool not in _TOOLS or not tid or not reference_id or not chunk_id:
                continue
            if not kb_name or not file_name or not isinstance(page_content, str) or not page_content:
                continue
            seen_ordinals.add(ordinal)
            rows.append((message_index, {
                "kind": "knowledge_base_document",
                "resource_type": "knowledge_base_document",
                "kb_name": kb_name,
                "file_name": file_name,
                "reference_id": reference_id,
                "chunks": [{
                    "chunk_id": chunk_id,
                    "page_content": page_content,
                    "score": item.get("score", ""),
                    "_source_tool": tool,
                    "_source_tid": tid,
                    "_row_index": 0,
                    "_chunk_index": 0,
                    "_immutable_chunk_id": False,
                }],
                "sources": [{"tool": tool, "tid": tid}],
            }))
    return rows


__all__ = [
    "CandidateRegistry",
    "CandidateScope",
    "CitationTokenStreamFilter",
    "CitationCandidate",
    "CitationSettlement",
    "KnowledgeBaseCitationHook",
    "annotate_tool_content_for_provider",
    "attach_settlement_to_message",
    "extract_committed_reference_rows",
    "settle_final_assistant",
    "strip_internal_citation_markers",
    "strip_orphan_citation_markers",
]

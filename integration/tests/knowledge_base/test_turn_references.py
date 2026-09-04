import json
import re

from integration.knowledge_base.turn_references import (
    ACROSS_SEARCH_TOOL,
    SINGLE_SEARCH_TOOL,
    extract_references,
    merge_reference_rows,
    to_wire,
)


def _preview(rows):
    return json.dumps({"result": json.dumps(rows, ensure_ascii=False)}, ensure_ascii=False)


def _document(kb_name, file_name, chunks):
    return {
        "metadata": {"kbName": kb_name, "fileName": file_name},
        "type": "Document",
        "chunks": chunks,
    }


def _untrusted_result(payload):
    return (
        '<untrusted_tool_result source="mcp__ithink_kb_mcp__searchKnowledgeBaseDocuments">\n'
        'The following content was retrieved from an untrusted tool.\n'
        f'{payload}\n'
        '</untrusted_tool_result>'
    )


def _assert_wire_identity(wire, *, tool, tid, chunk_count):
    assert wire["kind"] == "knowledge_base_document"
    assert re.fullmatch(r"kbdoc:v1:[A-Za-z0-9_-]{43}", wire["id"])
    assert wire["source"] == [{"tool": tool, "tid": tid}]
    chunks = wire["metadata"]["chunks"]
    assert len(chunks) == chunk_count
    assert all(re.fullmatch(r"kbchunk:v1:[A-Za-z0-9_-]{43}", c["id"]) for c in chunks)
    assert all(set(c) == {"id", "page_content", "score"} for c in chunks)


def test_single_search_reads_document_identity_and_chunks_from_result():
    rows = extract_references(
        name=SINGLE_SEARCH_TOOL,
        args={},
        result=_preview([_document("share49", "rules.docx", [
            {"page_content": "single passage", "score": 0.5},
            {"page_content": "second passage", "score": 0.4},
        ])]),
        tid="single-call",
    )

    wire = to_wire(rows[0])
    _assert_wire_identity(wire, tool=SINGLE_SEARCH_TOOL, tid="single-call", chunk_count=2)
    assert [c["page_content"] for c in wire["metadata"]["chunks"]] == ["single passage", "second passage"]


def test_single_search_unwraps_one_structured_result_from_agent_safety_wrapper():
    rows = extract_references(
        name=SINGLE_SEARCH_TOOL,
        args={},
        result=_untrusted_result(_preview([_document("share49", "rules.docx", [
            {"page_content": "wrapped passage", "score": 0.25},
        ])])),
        tid="wrapped-call",
    )

    wire = to_wire(rows[0])
    _assert_wire_identity(wire, tool=SINGLE_SEARCH_TOOL, tid="wrapped-call", chunk_count=1)
    assert wire["metadata"]["chunks"][0]["page_content"] == "wrapped passage"


def test_across_and_single_results_merge_same_document_and_keep_both_sources():
    across = extract_references(
        name=ACROSS_SEARCH_TOOL,
        args={},
        result=_preview([_document("share49", "rules.docx", [
            {"page_content": "first passage", "score": 0.2},
        ])]),
        tid="across-call",
    )
    single = extract_references(
        name=SINGLE_SEARCH_TOOL,
        args={},
        result=_preview([_document("share49", "rules.docx", [
            {"page_content": "second passage", "score": 0.4},
        ])]),
        tid="single-call",
    )

    merged = merge_reference_rows(across, single)

    wire = to_wire(merged[0])
    assert wire["source"] == [
        {"tool": ACROSS_SEARCH_TOOL, "tid": "across-call"},
        {"tool": SINGLE_SEARCH_TOOL, "tid": "single-call"},
    ]
    assert len(wire["metadata"]["chunks"]) == 2


def test_same_chunk_keeps_first_valid_score_and_rejects_invalid_score():
    first = extract_references(
        name=ACROSS_SEARCH_TOOL,
        args={},
        result=_preview([_document("share49", "rules.docx", [
            {"page_content": "same passage", "score": 0.2},
        ])]),
        tid="first-call",
    )
    second = extract_references(
        name=ACROSS_SEARCH_TOOL,
        args={},
        result=_preview([_document("share49", "rules.docx", [
            {"page_content": "same passage", "score": "not-a-number"},
        ])]),
        tid="second-call",
    )

    merged = merge_reference_rows(first, second)

    chunks = to_wire(merged[0])["metadata"]["chunks"]
    assert len(chunks) == 2
    assert {c["score"] for c in chunks} == {"", 0.2}


def test_missing_score_is_emitted_as_empty_string():
    rows = extract_references(
        name=ACROSS_SEARCH_TOOL,
        args={},
        result=_preview([_document("share49", "rules.docx", [
            {"page_content": "without score"},
        ])]),
        tid="missing-score-call",
    )

    chunks = to_wire(rows[0])["metadata"]["chunks"]
    assert len(chunks) == 1
    assert chunks[0]["page_content"] == "without score"
    assert chunks[0]["score"] == ""


def test_duplicate_chunk_upgrades_missing_score_to_first_valid_score():
    first = extract_references(
        name=ACROSS_SEARCH_TOOL,
        args={},
        result=_preview([_document("share49", "rules.docx", [
            {"page_content": "same passage", "score": "not-a-number"},
        ])]),
        tid="first-call",
    )
    second = extract_references(
        name=SINGLE_SEARCH_TOOL,
        args={},
        result=_preview([_document("share49", "rules.docx", [
            {"page_content": "same passage", "score": 0.8},
        ])]),
        tid="second-call",
    )

    merged = merge_reference_rows(first, second)

    chunks = to_wire(merged[0])["metadata"]["chunks"]
    assert len(chunks) == 2
    assert {c["score"] for c in chunks} == {"", 0.8}


def test_unknown_failed_or_malformed_results_fail_closed():
    assert extract_references(
        name="mcp__other__searchKnowledgeBaseDocuments",
        args={"kbName": "share49"}, result="[]", tid="x",
    ) == []
    assert extract_references(
        name=SINGLE_SEARCH_TOOL,
        args={}, result="[]", status="failed", tid="x",
    ) == []
    assert extract_references(
        name=SINGLE_SEARCH_TOOL,
        args={}, result="not json", tid="x",
    ) == []
    assert extract_references(
        name=SINGLE_SEARCH_TOOL,
        args={},
        result=_untrusted_result(_preview([]) + _preview([])),
        tid="x",
    ) == []
    assert extract_references(
        name=SINGLE_SEARCH_TOOL,
        args={"kbName": "share49"},
        result=_preview([{
            "page_content": "legacy flat result",
            "metadata": {"source": "/private/rules.docx"},
            "score": 0.5,
        }]),
        tid="x",
    ) == []
    assert extract_references(
        name=SINGLE_SEARCH_TOOL,
        args={},
        result=_preview([_document("share49", "rules.docx", [])]),
        tid="x",
    ) == []

import json

from integration.knowledge_base.turn_references import (
    ACROSS_SEARCH_TOOL,
    SINGLE_SEARCH_TOOL,
    extract_references,
    merge_reference_rows,
    to_wire,
)


def _preview(rows):
    return json.dumps({"result": json.dumps(rows, ensure_ascii=False)}, ensure_ascii=False)


def _untrusted_result(payload):
    return (
        '<untrusted_tool_result source="mcp__ithink_kb_mcp__searchKnowledgeBaseDocuments">\n'
        'The following content was retrieved from an untrusted tool.\n'
        f'{payload}\n'
        '</untrusted_tool_result>'
    )


def test_single_search_uses_args_kb_name_and_source_basename():
    rows = extract_references(
        name=SINGLE_SEARCH_TOOL,
        args={"kbName": "share49"},
        result=_preview([{
            "page_content": "single passage",
            "metadata": {"source": "/private/knowledge_base/share49/content/rules.docx"},
            "type": "Document",
            "score": 0.5,
        }]),
        tid="single-call",
    )

    assert to_wire(rows[0]) == {
        "kind": "knowledge_base_document",
        "source": [{"tool": SINGLE_SEARCH_TOOL, "tid": "single-call"}],
        "metadata": {
            "kbName": "share49",
            "fileName": "rules.docx",
            "chunks": [{"page_content": "single passage", "score": 0.5}],
        },
    }


def test_single_search_unwraps_one_structured_result_from_agent_safety_wrapper():
    rows = extract_references(
        name=SINGLE_SEARCH_TOOL,
        args={"kbName": "share49"},
        result=_untrusted_result(_preview([{
            "page_content": "wrapped passage",
            "metadata": {"source": "/private/knowledge_base/share49/content/rules.docx"},
            "score": 0.25,
        }])),
        tid="wrapped-call",
    )

    assert to_wire(rows[0]) == {
        "kind": "knowledge_base_document",
        "source": [{"tool": SINGLE_SEARCH_TOOL, "tid": "wrapped-call"}],
        "metadata": {
            "kbName": "share49",
            "fileName": "rules.docx",
            "chunks": [{"page_content": "wrapped passage", "score": 0.25}],
        },
    }


def test_across_and_single_results_merge_same_document_and_keep_both_sources():
    across = extract_references(
        name=ACROSS_SEARCH_TOOL,
        args={},
        result=_preview([{
            "page_content": "first passage",
            "metadata": {"kbName": "share49", "fileName": "rules.docx"},
            "score": 0.2,
        }]),
        tid="across-call",
    )
    single = extract_references(
        name=SINGLE_SEARCH_TOOL,
        args={"kbName": "share49"},
        result=_preview([{
            "page_content": "second passage",
            "metadata": {"source": "/private/rules.docx"},
            "score": 0.4,
        }]),
        tid="single-call",
    )

    merged = merge_reference_rows(across, single)

    assert to_wire(merged[0]) == {
        "kind": "knowledge_base_document",
        "source": [
            {"tool": ACROSS_SEARCH_TOOL, "tid": "across-call"},
            {"tool": SINGLE_SEARCH_TOOL, "tid": "single-call"},
        ],
        "metadata": {
            "kbName": "share49",
            "fileName": "rules.docx",
            "chunks": [
                {"page_content": "first passage", "score": 0.2},
                {"page_content": "second passage", "score": 0.4},
            ],
        },
    }


def test_same_chunk_keeps_first_valid_score_and_rejects_invalid_score():
    first = extract_references(
        name=ACROSS_SEARCH_TOOL,
        args={},
        result=_preview([{
            "page_content": "same passage",
            "metadata": {"kbName": "share49", "fileName": "rules.docx"},
            "score": 0.2,
        }]),
        tid="first-call",
    )
    second = extract_references(
        name=ACROSS_SEARCH_TOOL,
        args={},
        result=_preview([{
            "page_content": "same passage",
            "metadata": {"kbName": "share49", "fileName": "rules.docx"},
            "score": "not-a-number",
        }]),
        tid="second-call",
    )

    merged = merge_reference_rows(first, second)

    assert to_wire(merged[0])["metadata"]["chunks"] == [
        {"page_content": "same passage", "score": 0.2},
    ]


def test_missing_score_is_emitted_as_empty_string():
    rows = extract_references(
        name=ACROSS_SEARCH_TOOL,
        args={},
        result=_preview([{
            "page_content": "without score",
            "metadata": {"kbName": "share49", "fileName": "rules.docx"},
        }]),
        tid="missing-score-call",
    )

    assert to_wire(rows[0])["metadata"]["chunks"] == [
        {"page_content": "without score", "score": ""},
    ]


def test_unknown_failed_or_malformed_results_fail_closed():
    assert extract_references(
        name="mcp__other__searchKnowledgeBaseDocuments",
        args={"kbName": "share49"}, result="[]", tid="x",
    ) == []
    assert extract_references(
        name=SINGLE_SEARCH_TOOL,
        args={"kbName": "share49"}, result="[]", status="failed", tid="x",
    ) == []
    assert extract_references(
        name=SINGLE_SEARCH_TOOL,
        args={"kbName": "share49"}, result="not json", tid="x",
    ) == []
    assert extract_references(
        name=SINGLE_SEARCH_TOOL,
        args={"kbName": "share49"},
        result=_untrusted_result(_preview([]) + _preview([])),
        tid="x",
    ) == []

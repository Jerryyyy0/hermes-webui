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
            "page_content": ["single passage"],
        },
    }


def test_single_search_unwraps_one_structured_result_from_agent_safety_wrapper():
    rows = extract_references(
        name=SINGLE_SEARCH_TOOL,
        args={"kbName": "share49"},
        result=_untrusted_result(_preview([{
            "page_content": "wrapped passage",
            "metadata": {"source": "/private/knowledge_base/share49/content/rules.docx"},
        }])),
        tid="wrapped-call",
    )

    assert to_wire(rows[0]) == {
        "kind": "knowledge_base_document",
        "source": [{"tool": SINGLE_SEARCH_TOOL, "tid": "wrapped-call"}],
        "metadata": {
            "kbName": "share49",
            "fileName": "rules.docx",
            "page_content": ["wrapped passage"],
        },
    }


def test_across_and_single_results_merge_same_document_and_keep_both_sources():
    across = extract_references(
        name=ACROSS_SEARCH_TOOL,
        args={},
        result=_preview([{
            "page_content": "first passage",
            "metadata": {"kbName": "share49", "fileName": "rules.docx"},
        }]),
        tid="across-call",
    )
    single = extract_references(
        name=SINGLE_SEARCH_TOOL,
        args={"kbName": "share49"},
        result=_preview([{
            "page_content": "second passage",
            "metadata": {"source": "/private/rules.docx"},
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
            "page_content": ["first passage", "second passage"],
        },
    }


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

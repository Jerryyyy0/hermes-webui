import json

from integration.knowledge_base.citations import (
    CandidateRegistry,
    CandidateScope,
    CitationTokenStreamFilter,
    KnowledgeBaseCitationHook,
    extract_committed_reference_rows,
)
from api.helpers import public_session_projection, strip_knowledge_base_citations_for_copy
from integration.knowledge_base.turn_references import SINGLE_SEARCH_TOOL


def _result(chunks):
    rows = [{
        "type": "Document",
        "metadata": {"kbName": "kb", "fileName": "rules.md"},
        "chunks": chunks,
    }]
    return json.dumps({"result": json.dumps(rows, ensure_ascii=False)}, ensure_ascii=False)


def test_provider_annotation_is_chunk_scoped_and_preserves_wrapper():
    scope = CandidateScope("default", "session", "stream", 1, "turn:1")
    registry = CandidateRegistry(scope)
    registry.add_completed_result(
        name=SINGLE_SEARCH_TOOL,
        args={},
        result=_result([
            {"page_content": "same", "score": 0.1},
            {"page_content": "same", "score": 0.2},
        ]),
        tid="call-1",
    )
    hook = KnowledgeBaseCitationHook(registry)
    annotated = hook.annotate_tool_content_for_provider(
        tool_call_id="call-1",
        tool_name=SINGLE_SEARCH_TOOL,
        content=(
            '<untrusted_tool_result source="mcp__ithink_kb_mcp__searchKnowledgeBaseDocuments">'
            + _result([
                {"page_content": "same", "score": 0.1},
                {"page_content": "same", "score": 0.2},
            ])
            + "</untrusted_tool_result>"
        ),
    )
    assert annotated.startswith("<untrusted_tool_result")
    assert annotated.endswith("</untrusted_tool_result>")
    assert annotated.count('_cite') == 2
    payload = json.loads(annotated.split(">", 1)[1].rsplit("</untrusted_tool_result>", 1)[0])
    chunks = json.loads(payload["result"])[0]["chunks"]
    assert [chunk["score"] for chunk in chunks] == [0.2, 0.1]
    assert all(chunk.get("_cite") for chunk in chunks)


def test_settlement_replaces_only_model_selected_chunk_and_exposes_mapping():
    scope = CandidateScope("default", "session", "stream", 1, "turn:1")
    registry = CandidateRegistry(scope)
    candidates = registry.add_completed_result(
        name=SINGLE_SEARCH_TOOL,
        args={},
        result=_result([
            {"page_content": "first", "score": 0.1},
            {"page_content": "second", "score": 0.2},
        ]),
        tid="call-2",
    )
    hook = KnowledgeBaseCitationHook(registry)
    prepared = hook.prepare_final_assistant(raw_content=f"结论 [[c:{candidates[1].token}]]")
    assert prepared["content"] == '结论 <sup data-c="1">[1]</sup>'
    settlement = hook.settlement(prepared["settlement_id"])
    assert settlement is not None
    assert settlement.citations[0]["chunk_ids"] == [candidates[1].chunk_id]
    assert settlement.citations[0]["ordinal"] == 1


def test_settlement_reuses_ordinal_for_repeated_chunk_token():
    scope = CandidateScope("default", "session-repeat", "stream", 1, "turn:1")
    registry = CandidateRegistry(scope)
    candidate = registry.add_completed_result(
        name=SINGLE_SEARCH_TOOL,
        args={},
        result=_result([{"page_content": "shared evidence", "score": 0.5}]),
        tid="call-repeat",
    )[0]
    hook = KnowledgeBaseCitationHook(registry)

    prepared = hook.prepare_final_assistant(
        raw_content=(
            f"第一项结论。[[c:{candidate.token}]] "
            f"第二项结论。[[c:{candidate.token}]]"
        )
    )
    settlement = hook.settlement(prepared["settlement_id"])

    assert prepared["content"] == (
        '第一项结论。<sup data-c="1">[1]</sup> '
        '第二项结论。<sup data-c="1">[1]</sup>'
    )
    assert settlement is not None
    assert len(settlement.citations) == 1
    assert len(settlement.evidence) == 1
    assert settlement.citations[0]["ordinal"] == 1
    assert settlement.citations[0]["chunk_ids"] == [candidate.chunk_id]


def test_settlement_sorts_adjacent_markers_by_existing_ordinal():
    scope = CandidateScope("default", "session-marker-order", "stream", 1, "turn:1")
    registry = CandidateRegistry(scope)
    first, second = registry.add_completed_result(
        name=SINGLE_SEARCH_TOOL,
        args={},
        result=_result([
            {"page_content": "first evidence", "score": 0.5},
            {"page_content": "second evidence", "score": 0.4},
        ]),
        tid="call-marker-order",
    )
    hook = KnowledgeBaseCitationHook(registry)

    prepared = hook.prepare_final_assistant(
        raw_content=(
            f"前一项。[[c:{first.token}]] "
            f"当前项。[[c:{second.token}]] [[c:{first.token}]]"
        )
    )
    settlement = hook.settlement(prepared["settlement_id"])

    assert prepared["content"] == (
        '前一项。<sup data-c="1">[1]</sup> '
        '当前项。<sup data-c="1">[1]</sup><sup data-c="2">[2]</sup>'
    )
    assert settlement is not None
    assert [citation["ordinal"] for citation in settlement.citations] == [1, 2]


def test_public_projection_requires_self_consistent_message_citation():
    scope = CandidateScope("default", "session-public", "stream", 1, "turn:1")
    registry = CandidateRegistry(scope)
    candidate = registry.add_completed_result(
        name=SINGLE_SEARCH_TOOL,
        args={},
        result=_result([{"page_content": "public", "score": 1}]),
        tid="call-public",
    )[0]
    hook = KnowledgeBaseCitationHook(registry)
    prepared = hook.prepare_final_assistant(raw_content=f"结论 [[c:{candidate.token}]]")
    settlement = hook.settlement(prepared["settlement_id"])
    message = {"role": "assistant", "content": prepared["content"]}
    assert settlement is not None
    message.update({"citations": list(settlement.citations), "_knowledge_base_citation_evidence": list(settlement.evidence)})
    message["citations"][0]["chunk_ids"] = ["wrong"]
    invalid = public_session_projection({"session_id": "session-public", "messages": [message]})
    assert invalid["messages"][0].get("citations") is None
    assert "data-c=\"1\"" not in invalid["messages"][0]["content"]
    message["citations"][0]["chunk_ids"] = [candidate.chunk_id]
    hook.mark_attached_pending(prepared["settlement_id"])
    hook.commit(prepared["settlement_id"])
    committed = public_session_projection({"session_id": "session-public", "messages": [message]})
    assert committed["messages"][0]["citations"][0]["chunk_ids"] == [candidate.chunk_id]
    assert "_knowledge_base_citation_evidence" not in committed["messages"][0]


def test_copy_projection_drops_old_session_citation_mapping():
    copied = strip_knowledge_base_citations_for_copy([{
        "role": "assistant",
        "content": "结论。<sup data-c=\"1\">[1]</sup>",
        "citations": [{"ordinal": 1, "reference_id": "old"}],
        "_knowledge_base_citation_evidence": [{"reference_id": "old"}],
    }])
    assert copied[0]["content"] == "结论。"
    assert "citations" not in copied[0]
    assert "_knowledge_base_citation_evidence" not in copied[0]


def test_stream_filter_suppresses_marker_split_across_deltas():
    stream_filter = CitationTokenStreamFilter()
    parts = ["结论 [[", "c:K7x8pQm2", "Vt4zAa9B]] 后文"]

    visible = "".join(stream_filter.feed(part) for part in parts) + stream_filter.finish()

    assert visible == "结论  后文"

    truncated = CitationTokenStreamFilter()
    assert truncated.feed("结论 [[c:K7x8pQm2") == "结论 "
    assert truncated.finish() == ""


def test_committed_reference_rows_require_marker_citation_and_evidence():
    scope = CandidateScope("default", "session-manifest", "stream", 1, "turn:1")
    registry = CandidateRegistry(scope)
    candidate = registry.add_completed_result(
        name=SINGLE_SEARCH_TOOL,
        args={},
        result=_result([{"page_content": "selected", "score": 0.7}]),
        tid="call-manifest",
    )[0]
    hook = KnowledgeBaseCitationHook(registry)
    prepared = hook.prepare_final_assistant(raw_content=f"结论 [[c:{candidate.token}]]")
    settlement = hook.settlement(prepared["settlement_id"])
    assert settlement is not None
    message = {
        "role": "assistant",
        "content": prepared["content"],
        "citations": list(settlement.citations),
        "_knowledge_base_citation_evidence": list(settlement.evidence),
    }

    rows = extract_committed_reference_rows([message])

    assert rows[0][0] == 0
    assert rows[0][1]["reference_id"] == candidate.reference_id
    assert rows[0][1]["chunks"][0]["chunk_id"] == candidate.chunk_id
    message["content"] = "结论"
    assert extract_committed_reference_rows([message]) == []

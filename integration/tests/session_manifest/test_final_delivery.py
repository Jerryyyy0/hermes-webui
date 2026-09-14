"""Final visible reply is sufficient artifact evidence, including read inputs."""
import json
import os
from types import SimpleNamespace

import pytest

from integration.session_manifest.manifest import (
    _paths_from_assistant_media,
    _paths_from_last_assistant_message,
    extract_turn_artifact_entries_for_manifest,
    extract_manifest_delta_from_turn_reconcile,
)


def test_read_execute_code_final_delivery(tmp_path):
    # Shape of session 192ce1415d98 turn:4: read CSV, execute_code rewrites
    # CSV/MP4, final reply names both (execute_code is not a mutation tool).
    for name in ('公开知识库列表.csv', '公开知识库列表.mp4'):
        (tmp_path / name).write_bytes(b'output')
    messages = [
        {'role': 'user', 'content': '修复 CSV 和 MP4', '_turn_key': 'turn:4'},
        {'role': 'assistant', 'tool_calls': [{'id': 'read', 'function': {
            'name': 'read_file', 'arguments': json.dumps({'path': str(tmp_path / '公开知识库列表.csv')})}}]},
        {'role': 'tool', 'tool_call_id': 'read', 'content': 'data'},
        {'role': 'assistant', 'tool_calls': [{'id': 'rewrite', 'function': {
            'name': 'execute_code', 'arguments': '{"code":"..."}'}}]},
        {'role': 'tool', 'tool_call_id': 'rewrite', 'content': '{"status":"success"}'},
        {'role': 'assistant', 'content': '已修复 `公开知识库列表.csv` 和 `公开知识库列表.mp4`'},
    ]
    session = SimpleNamespace(messages=messages, tool_calls=[], workspace=str(tmp_path), profile='default')
    assert {row['path'] for row in extract_turn_artifact_entries_for_manifest(session, 'turn:4')} == {
        '公开知识库列表.csv', '公开知识库列表.mp4'}
    delta = extract_manifest_delta_from_turn_reconcile(messages, tmp_path, turn_key='turn:4', sequence=1)
    assert {row['path'] for row in delta['artifacts']} == {'公开知识库列表.csv', '公开知识库列表.mp4'}


def test_media_line_preserves_spaces_in_local_path(tmp_path):
    artifact = tmp_path / '抒情 散文.docx'
    artifact.write_bytes(b'output')

    assert _paths_from_assistant_media(
        f'MEDIA:{artifact}', tmp_path,
    ) == ['抒情 散文.docx']
    assert _paths_from_assistant_media(
        f'下载 MEDIA:<{artifact}>', tmp_path,
    ) == ['抒情 散文.docx']
    assert _paths_from_assistant_media(
        f'MEDIA:{artifact}\r\n校验通过', tmp_path,
    ) == ['抒情 散文.docx']


def test_inline_legacy_media_keeps_whitespace_boundary(tmp_path):
    artifact = tmp_path / 'report.docx'
    artifact.write_bytes(b'output')

    assert _paths_from_assistant_media(
        f'查看 MEDIA:{artifact} 下载', tmp_path,
    ) == ['report.docx']


def test_final_bold_bare_filename_preserves_spaces(tmp_path):
    artifact = tmp_path / '抒情 散文.docx'
    artifact.write_bytes(b'output')

    assert _paths_from_last_assistant_message(
        '已生成 **抒情 散文.docx**：A4 两页。', tmp_path,
    ) == ['抒情 散文.docx']


def test_final_bold_prose_is_not_a_file_candidate(tmp_path):
    (tmp_path / '转换成功').write_bytes(b'not-an-artifact')

    assert _paths_from_last_assistant_message(
        '**转换成功**，已生成 Word 文档。', tmp_path,
    ) == []


def test_reported_spaced_docx_delivery_is_registered_once(tmp_path):
    artifact = tmp_path / '抒情 散文.docx'
    artifact.write_bytes(b'output')
    messages = [
        {'role': 'user', 'content': '帮我转为word', '_turn_key': 'turn:2'},
        {
            'role': 'assistant',
            'content': (
                '校验通过：中文文本层完整、A4 两页、无乱码。\n\n'
                f'MEDIA:{artifact}\n\n'
                '已生成 **抒情 散文.docx**：标题宋体 18pt。'
            ),
        },
    ]

    delta = extract_manifest_delta_from_turn_reconcile(
        messages, tmp_path, turn_key='turn:2', sequence=1,
    )

    assert delta['turns'] == [{
        'turn_key': 'turn:2',
        'artifacts': [{
            'path': '抒情 散文.docx',
            'preview': 'file',
            'source_tool': 'media',
        }],
        'references': [],
    }]


def test_unlimited_deliveries(tmp_path):
    names = [f'report-{i}.pdf' for i in range(65)]
    for name in names:
        (tmp_path / name).write_bytes(b'x')
    assert set(_paths_from_last_assistant_message(' '.join(names), tmp_path)) == set(names)


def test_newest_ties_are_all_delivered(tmp_path):
    for folder, stamp in [('a', 20), ('b', 20), ('old', 10)]:
        path = tmp_path / folder / 'report.pdf'
        path.parent.mkdir()
        path.write_bytes(b'x')
        os.utime(path, ns=(stamp, stamp))
    assert set(_paths_from_last_assistant_message('report.pdf', tmp_path)) == {'a/report.pdf', 'b/report.pdf'}


@pytest.mark.parametrize('text,expected', [
    ('`Dockerfile`', {'Dockerfile'}),
    ('src/a.c', {'src/a.c'}),
    ('`销售 报告.pdf`', {'销售 报告.pdf'}),
    ('[报告](<销售 报告.pdf>)', {'销售 报告.pdf'}),
    ('[report.pdf](https://example.org/report.pdf)', set()),
    ('[report.pdf](https://example.org/report.pdf)请查看', set()),
    ('[report.pdf]\n\n[report.pdf]: https://example.org/report.pdf', set()),
    ('[report.pdf](missing.pdf)', set()),
    ('https://example.org/report.pdf', set()),
    ('Dockerfile', set()),
    ('"Dockerfile"', {'Dockerfile'}),
    ('[report.pdf][remote]\n\n[remote]: https://example.org/report.pdf', set()),
    ('[说明][local]\n\n[local]: report.pdf', {'report.pdf'}),
])
def test_explicit_path_boundaries(tmp_path, text, expected):
    for name in ['Dockerfile', 'src/a.c', '销售 报告.pdf', 'report.pdf']:
        path = tmp_path / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b'x')
    assert set(_paths_from_last_assistant_message(text, tmp_path)) == expected


def test_store_keeps_stronger_source_on_later_prose(tmp_path, monkeypatch):
    from integration.session_manifest.store import upsert_manifest_records, load_manifest_records
    monkeypatch.setattr('integration.session_manifest.store.STATE_DIR', tmp_path / 'state')
    session = SimpleNamespace(session_id='priority', profile='default', workspace=str(tmp_path))
    for source in ['terminal', 'media', 'assistant_prose']:
        upsert_manifest_records(session, 'turn:0', [{'path': 'report.pdf', 'source_tool': source}])
    assert load_manifest_records(session)[0]['source_tool'] == 'terminal'


def test_late_reply_reconcile_uses_origin_turn(tmp_path):
    for name in ['old.pdf', 'new.pdf']:
        (tmp_path / name).write_bytes(b'x')
    messages = [
        {'role': 'user', 'content': 'old', '_turn_key': 'turn:0'},
        {'role': 'assistant', 'content': 'working'},
        {'role': 'user', 'content': 'new', '_turn_key': 'turn:2'},
        {'role': 'assistant', 'content': '`new.pdf`'},
        {'role': 'assistant', 'content': '`old.pdf`', '_turn_key': 'turn:0'},
    ]
    for key, expected in [('turn:0', 'old.pdf'), ('turn:2', 'new.pdf')]:
        delta = extract_manifest_delta_from_turn_reconcile(messages, tmp_path, turn_key=key)
        assert [row['path'] for row in delta['turns'][0]['artifacts']] == [expected]


@pytest.mark.parametrize('terminal_reason', ['completed', 'error', 'cancelled'])
def test_final_delivery_settlement_on_every_terminal(tmp_path, monkeypatch, terminal_reason):
    from api.models import Session
    from api.streaming import _persist_turn_artifact_paths
    from integration.session_manifest.store import load_manifest_records
    monkeypatch.setattr('integration.session_manifest.store.STATE_DIR', tmp_path / 'state')
    (tmp_path / 'result.pdf').write_bytes(b'x')
    session = Session(session_id='terminal-matrix', workspace=str(tmp_path), messages=[
        {'role': 'user', 'content': 'deliver', '_turn_key': 'turn:0'},
        {'role': 'assistant', 'content': '`result.pdf`'},
        {'role': 'assistant', 'content': '', 'tool_calls': []},
        {'role': 'assistant', 'content': 'internal', '_hermes_message_class': 'internal_scaffold'},
    ])
    assert _persist_turn_artifact_paths(session, 'turn:0', terminal_reason=terminal_reason)['status'] == 'persisted'
    assert [row['path'] for row in load_manifest_records(session)] == ['result.pdf']

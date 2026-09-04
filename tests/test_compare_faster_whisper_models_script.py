"""Unit coverage for the local faster-whisper comparison script."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import json

import pytest


_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "compare_faster_whisper_models.py"
_SPEC = importlib.util.spec_from_file_location("compare_faster_whisper_models", _SCRIPT)
assert _SPEC and _SPEC.loader
compare = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = compare
_SPEC.loader.exec_module(compare)


def test_parse_model_list_deduplicates_and_preserves_order():
    assert compare.parse_model_list(" base,small,base , medium ") == ["base", "small", "medium"]


def test_parse_model_list_rejects_empty_values():
    with pytest.raises(Exception, match="至少提供一个模型名称"):
        compare.parse_model_list(" , , ")


def test_parse_args_uses_sample_directory_when_audio_is_omitted():
    args = compare.parse_args([])
    assert args.audio == compare.DEFAULT_AUDIO_DIRECTORY
    assert args.funasr_url == compare.DEFAULT_FUNASR_URL
    assert args.skip_funasr is False


def test_character_error_rate_normalizes_whitespace():
    assert compare.character_error_rate("你好  世界", "你好 世界") == 0
    assert compare.character_error_rate("你好世界", "你好") == pytest.approx(0.5)


def test_collect_audio_files_reads_supported_files_recursively(tmp_path):
    (tmp_path / "one.webm").write_bytes(b"audio")
    (tmp_path / "ignore.txt").write_text("not audio", encoding="utf-8")
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "two.m4a").write_bytes(b"audio")

    assert compare.collect_audio_files(tmp_path) == [
        (nested / "two.m4a").resolve(),
        (tmp_path / "one.webm").resolve(),
    ]


def test_print_result_includes_audio_path_and_transcript(capsys):
    result = compare.ModelResult(
        audio="/tmp/speech.webm",
        model="base",
        status="ok",
        load_seconds=0.1,
        transcribe_seconds=0.2,
        audio_seconds=1.5,
        transcript="你好",
    )

    compare._print_result(result, include_transcript=True, include_audio=False)

    output = capsys.readouterr().out
    assert "模型: base" in output
    assert "文件:" not in output
    assert "源文件:" not in output
    assert "转写结果:\n你好" in output


def test_transcribe_funasr_checks_health_and_parses_response(monkeypatch, tmp_path):
    audio = tmp_path / "speech.wav"
    audio.write_bytes(b"RIFFfake")
    responses = [
        {"status": "ok", "loaded": True},
        {"text": "你好", "segments": [{"text": "你好", "start": 0.0, "end": 1.0}]},
    ]
    requests = []

    class FakeResponse:
        status = 200

        def __init__(self, payload):
            self.payload = json.dumps(payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return self.payload

    def fake_urlopen(request, timeout):
        requests.append((request, timeout))
        return FakeResponse(responses.pop(0))

    monkeypatch.setattr(compare.urllib.request, "urlopen", fake_urlopen)
    result = compare.transcribe_funasr(
        [audio], base_url="http://example.test/", language="zh", timeout=3
    )

    assert result[0].status == "ok"
    assert result[0].transcript == "你好"
    assert len(result[0].segments) == 1
    assert requests[0][0].full_url == "http://example.test/health"
    assert requests[1][0].full_url == "http://example.test/transcribe"
    assert requests[1][0].data

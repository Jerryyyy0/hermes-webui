#!/usr/bin/env python3
"""Compare local faster-whisper models against one audio file.

The script uses the same default inference settings as the WebUI local STT
path (``device=auto``, ``compute_type=auto``, and ``beam_size=5``). Models
must already be cached unless ``--allow-download`` is supplied explicitly.

Examples:
    python scripts/compare_faster_whisper_models.py \
        --models base,small,medium --language zh

    python scripts/compare_faster_whisper_models.py sample.webm \
        --models base,small --reference-file expected.txt --json-out result.json

    python scripts/compare_faster_whisper_models.py \
        --models small,medium --language zh \
        --funasr-url http://192.168.1.137:38080
"""
from __future__ import annotations

import argparse
import gc
import json
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


DEFAULT_MODELS = ("small", "medium")
SUPPORTED_AUDIO_SUFFIXES = frozenset(
    {".aac", ".flac", ".m4a", ".mp3", ".mp4", ".mpeg", ".mpga", ".ogg", ".wav", ".webm"}
)
DEFAULT_AUDIO_DIRECTORY = Path(__file__).resolve().parent / "faster_whisper_samples"
DEFAULT_FUNASR_URL = "http://192.168.1.137:38080"


@dataclass
class ModelResult:
    audio: str
    model: str
    status: str
    load_seconds: float | None = None
    transcribe_seconds: float | None = None
    audio_seconds: float | None = None
    detected_language: str | None = None
    language_probability: float | None = None
    transcript: str | None = None
    character_error_rate: float | None = None
    error: str | None = None


@dataclass
class FunASRResult:
    audio: str
    status: str
    transcribe_seconds: float | None = None
    transcript: str | None = None
    segments: list[dict] | None = None
    error: str | None = None


def parse_model_list(value: str) -> list[str]:
    """Parse a comma-separated model list while preserving caller order."""
    models: list[str] = []
    for raw_name in value.split(","):
        name = raw_name.strip()
        if name and name not in models:
            models.append(name)
    if not models:
        raise argparse.ArgumentTypeError("至少提供一个模型名称")
    return models


def normalize_reference_text(text: str) -> str:
    """Normalize whitespace so Chinese and spaced-language comparisons are stable."""
    return " ".join(text.strip().split())


def levenshtein_distance(left: str, right: str) -> int:
    """Return the edit distance without a third-party dependency."""
    if len(left) < len(right):
        left, right = right, left
    previous = list(range(len(right) + 1))
    for left_index, left_char in enumerate(left, start=1):
        current = [left_index]
        for right_index, right_char in enumerate(right, start=1):
            replace_cost = previous[right_index - 1] + (left_char != right_char)
            current.append(
                min(
                    previous[right_index] + 1,
                    current[right_index - 1] + 1,
                    replace_cost,
                )
            )
        previous = current
    return previous[-1]


def character_error_rate(reference: str, transcript: str) -> float | None:
    """Return CER for a non-empty reference, including Chinese text."""
    normalized_reference = normalize_reference_text(reference)
    normalized_transcript = normalize_reference_text(transcript)
    if not normalized_reference:
        return None
    return levenshtein_distance(normalized_reference, normalized_transcript) / len(
        normalized_reference
    )


def collect_audio_files(source: Path) -> list[Path]:
    """Return one supported audio file, or all supported files below a directory."""
    source = source.expanduser().resolve()
    if source.is_file():
        if source.suffix.lower() not in SUPPORTED_AUDIO_SUFFIXES:
            raise ValueError(f"不支持的音频扩展名：{source.suffix or '（无扩展名）'}")
        return [source]
    if not source.is_dir():
        raise ValueError(f"音频文件或目录不存在：{source}")
    return sorted(
        path.resolve()
        for path in source.rglob("*")
        if path.is_file() and path.suffix.lower() in SUPPORTED_AUDIO_SUFFIXES
    )


def _multipart_audio_body(audio_path: Path, language: str) -> tuple[bytes, str]:
    """Build the small multipart request needed by the FunASR endpoint."""
    boundary = f"----hermes-funasr-{time.time_ns()}"
    audio = audio_path.read_bytes()
    content_type = {
        ".m4a": "audio/mp4",
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".webm": "audio/webm",
        ".ogg": "audio/ogg",
    }.get(audio_path.suffix.lower(), "application/octet-stream")
    parts = [
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"language\"\r\n\r\n{language}\r\n".encode(),
        (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
            f"filename=\"{audio_path.name}\"\r\nContent-Type: {content_type}\r\n\r\n"
        ).encode()
        + audio
        + b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ]
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _funasr_request(url: str, request: urllib.request.Request, timeout: float) -> dict:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read().decode("utf-8")
            status = response.status
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"HTTP {exc.code}: {detail or exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"无法连接 {url}: {exc.reason}") from exc
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"响应不是 JSON（HTTP {status}）") from exc
    if not isinstance(data, dict):
        raise RuntimeError("响应 JSON 不是对象")
    return data


def transcribe_funasr(
    audio_paths: Iterable[Path], *, base_url: str, language: str, timeout: float
) -> list[FunASRResult]:
    """Transcribe files through the optional SenseVoice/FunASR HTTP service."""
    base_url = base_url.rstrip("/")
    health_request = urllib.request.Request(f"{base_url}/health", method="GET")
    health = _funasr_request(f"{base_url}/health", health_request, timeout)
    if health.get("status") != "ok" or not health.get("loaded"):
        raise RuntimeError(f"FunASR 健康检查未就绪: {health}")

    results: list[FunASRResult] = []
    for audio_path in audio_paths:
        try:
            body, content_type = _multipart_audio_body(audio_path, language)
            request = urllib.request.Request(
                f"{base_url}/transcribe",
                data=body,
                headers={"Content-Type": content_type},
                method="POST",
            )
            started = time.perf_counter()
            data = _funasr_request(f"{base_url}/transcribe", request, timeout)
            results.append(
                FunASRResult(
                    audio=str(audio_path),
                    status="ok",
                    transcribe_seconds=time.perf_counter() - started,
                    transcript=str(data.get("text") or "").strip(),
                    segments=data.get("segments") if isinstance(data.get("segments"), list) else [],
                )
            )
        except Exception as exc:
            results.append(
                FunASRResult(
                    audio=str(audio_path),
                    status="error",
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
    return results


def transcribe_models(
    audio_paths: Iterable[Path],
    models: Iterable[str],
    *,
    language: str | None,
    device: str,
    compute_type: str,
    beam_size: int,
    allow_download: bool,
    reference_text: str | None,
) -> list[ModelResult]:
    """Run every model serially and reuse it for all audio files."""
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError(
            "未安装 faster-whisper。请在运行 Hermes Agent 的同一环境中执行："
            "python -m pip install faster-whisper"
        ) from exc

    audio_paths = list(audio_paths)
    results: list[ModelResult] = []
    for model_name in models:
        model = None
        try:
            load_started = time.perf_counter()
            model = WhisperModel(
                model_name,
                device=device,
                compute_type=compute_type,
                local_files_only=not allow_download,
            )
            load_seconds = time.perf_counter() - load_started

            for audio_path in audio_paths:
                try:
                    transcribe_kwargs: dict[str, object] = {"beam_size": beam_size}
                    if language:
                        transcribe_kwargs["language"] = language
                    transcribe_started = time.perf_counter()
                    segments, info = model.transcribe(str(audio_path), **transcribe_kwargs)
                    transcript = " ".join(segment.text.strip() for segment in segments).strip()
                    transcribe_seconds = time.perf_counter() - transcribe_started
                    results.append(
                        ModelResult(
                            audio=str(audio_path),
                            model=model_name,
                            status="ok",
                            load_seconds=load_seconds,
                            transcribe_seconds=transcribe_seconds,
                            audio_seconds=float(getattr(info, "duration", 0.0) or 0.0),
                            detected_language=str(getattr(info, "language", "") or "") or None,
                            language_probability=getattr(info, "language_probability", None),
                            transcript=transcript,
                            character_error_rate=(
                                character_error_rate(reference_text, transcript)
                                if reference_text is not None
                                else None
                            ),
                        )
                    )
                except Exception as exc:
                    results.append(
                        ModelResult(
                            audio=str(audio_path),
                            model=model_name,
                            status="error",
                            error=f"{type(exc).__name__}: {exc}",
                        )
                    )
        except Exception as exc:
            results.extend(
                ModelResult(
                    audio=str(audio_path),
                    model=model_name,
                    status="error",
                    error=f"{type(exc).__name__}: {exc}",
                )
                for audio_path in audio_paths
            )
        finally:
            # Models can be several GB. Do not keep earlier models resident while
            # loading the next one, otherwise the comparison itself can cause OOM.
            if model is not None:
                del model
            gc.collect()

    return results


def _print_result(
    result: ModelResult, *, include_transcript: bool, include_audio: bool = True
) -> None:
    print(
        f"\n=== 文件: {result.audio} | 模型: {result.model} ==="
        if include_audio
        else f"\n--- 模型: {result.model} ---"
    )
    if result.status != "ok":
        print(f"状态: 失败\n原因: {result.error}")
        return
    print("状态: 成功")
    print(f"加载耗时: {result.load_seconds:.2f}s")
    print(f"转写耗时: {result.transcribe_seconds:.2f}s")
    print(f"音频时长: {result.audio_seconds:.2f}s")
    if result.detected_language:
        probability = (
            f"（置信度 {result.language_probability:.2%}）"
            if result.language_probability is not None
            else ""
        )
        print(f"识别语言: {result.detected_language}{probability}")
    if result.character_error_rate is not None:
        print(f"字符错误率 (CER): {result.character_error_rate:.2%}")
    if include_transcript:
        print(f"转写结果:\n{result.transcript or '（空）'}")


def _print_funasr_result(
    result: FunASRResult, *, include_transcript: bool, include_audio: bool = True
) -> None:
    print(
        "\n=== 文件: " + result.audio + " | 模型: SenseVoiceSmall (FunASR API) ==="
        if include_audio
        else "\n--- 模型: SenseVoiceSmall (FunASR API) ---"
    )
    if result.status != "ok":
        print(f"状态: 失败\n原因: {result.error}")
        return
    print("状态: 成功")
    print(f"转写耗时: {result.transcribe_seconds:.2f}s")
    print(f"分段数: {len(result.segments or [])}")
    if include_transcript:
        print(f"转写结果:\n{result.transcript or '（空）'}")
        for segment in result.segments or []:
            print(
                f"  [{segment.get('start', 0):.2f}s - {segment.get('end', 0):.2f}s] "
                f"{segment.get('text', '')}"
            )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "audio",
        nargs="?",
        type=Path,
        default=DEFAULT_AUDIO_DIRECTORY,
        help="待转写的音频文件或目录（默认：scripts/faster_whisper_samples/）",
    )
    parser.add_argument(
        "--models",
        type=parse_model_list,
        default=list(DEFAULT_MODELS),
        help="逗号分隔的模型列表（默认：tiny,base,small,medium,large-v3）",
    )
    parser.add_argument("--language", help="强制语言代码，例如 zh；默认自动识别")
    parser.add_argument(
        "--device", default="auto", help="faster-whisper device 参数（默认：auto）"
    )
    parser.add_argument(
        "--compute-type",
        default="auto",
        help="faster-whisper compute_type 参数（默认：auto）",
    )
    parser.add_argument(
        "--beam-size", type=int, default=5, help="解码 beam size（默认：5）"
    )
    parser.add_argument(
        "--reference-file", type=Path, help="可选的人工参考文本文件；用于计算 CER"
    )
    parser.add_argument(
        "--json-out", type=Path, help="可选的 JSON 报告输出路径；默认只在控制台打印结果"
    )
    parser.add_argument(
        "--no-transcripts", action="store_true", help="只打印指标，不打印完整转写文本"
    )
    parser.add_argument(
        "--allow-download",
        action="store_true",
        help="允许下载尚未缓存的模型；默认仅使用本地缓存",
    )
    parser.add_argument(
        "--funasr-url",
        default=DEFAULT_FUNASR_URL,
        help=f"SenseVoice/FunASR API 地址（默认：{DEFAULT_FUNASR_URL}）",
    )
    parser.add_argument(
        "--skip-funasr",
        action="store_true",
        help="跳过默认的 SenseVoice/FunASR API 验证",
    )
    parser.add_argument(
        "--funasr-timeout",
        type=float,
        default=120.0,
        help="FunASR 每个请求的超时时间（秒，默认：120）",
    )
    args = parser.parse_args(argv)
    if args.beam_size < 1:
        parser.error("--beam-size 必须大于或等于 1")
    if args.funasr_timeout <= 0:
        parser.error("--funasr-timeout 必须大于 0")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        audio_paths = collect_audio_files(args.audio)
    except ValueError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    if not audio_paths:
        print(f"错误：目录中没有支持的音频文件：{args.audio.expanduser()}", file=sys.stderr)
        return 2

    reference_text = None
    if args.reference_file:
        if len(audio_paths) != 1:
            print("错误：批量音频比较不支持单个 --reference-file", file=sys.stderr)
            return 2
        try:
            reference_text = args.reference_file.expanduser().read_text(
                encoding="utf-8"
            )
        except OSError as exc:
            print(f"错误：无法读取参考文本：{exc}", file=sys.stderr)
            return 2

    try:
        results = transcribe_models(
            audio_paths,
            args.models,
            language=args.language,
            device=args.device,
            compute_type=args.compute_type,
            beam_size=args.beam_size,
            allow_download=args.allow_download,
            reference_text=reference_text,
        )
    except RuntimeError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    funasr_results: list[FunASRResult] = []
    if args.funasr_url and not args.skip_funasr:
        try:
            funasr_results = transcribe_funasr(
                audio_paths,
                base_url=args.funasr_url,
                language=args.language or "auto",
                timeout=args.funasr_timeout,
            )
        except RuntimeError as exc:
            print(f"\nFunASR 服务验证失败: {exc}", file=sys.stderr)
            return 2
    # Group output by source file so model results for one recording are easy
    # to compare side by side in the console.
    for audio_path in audio_paths:
        audio_name = str(audio_path)
        print(f"\n源文件: {audio_name}")
        for result in results:
            if result.audio == audio_name:
                _print_result(
                    result,
                    include_transcript=not args.no_transcripts,
                    include_audio=False,
                )
        for result in funasr_results:
            if result.audio == audio_name:
                _print_funasr_result(
                    result,
                    include_transcript=not args.no_transcripts,
                    include_audio=False,
                )

    if args.json_out:
        report = {
            "audio_files": [str(path) for path in audio_paths],
            "models": args.models,
            "language": args.language,
            "device": args.device,
            "compute_type": args.compute_type,
            "beam_size": args.beam_size,
            "reference_file": str(args.reference_file) if args.reference_file else None,
            "results": [asdict(result) for result in results],
            "funasr_url": None if args.skip_funasr else args.funasr_url,
            "funasr_results": [asdict(result) for result in funasr_results],
        }
        try:
            args.json_out.expanduser().write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            print(f"\nJSON 报告: {args.json_out.expanduser()}")
        except OSError as exc:
            print(f"错误：无法写入 JSON 报告：{exc}", file=sys.stderr)
            return 2

    succeeded = any(result.status == "ok" for result in results) or any(
        result.status == "ok" for result in funasr_results
    )
    return 0 if succeeded else 1


if __name__ == "__main__":
    raise SystemExit(main())

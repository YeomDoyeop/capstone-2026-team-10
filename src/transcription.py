import os
import tempfile
import threading
from dataclasses import replace
from pathlib import Path
from collections.abc import Callable
from typing import Any

import whisperx
from whisperx.audio import SAMPLE_RATE

from .input_audio import AudioDownloadError, apply_speed, download_audio
from .request import RequestValidationError, parse_request
from .timestamps import restore_original_segment_timestamps

MODEL_NAME = "large-v3"
DEVICE = "cuda"
COMPUTE_TYPE = "float16"
BATCH_SIZE = int(os.environ.get("WHISPERX_BATCH_SIZE", "16"))
MODEL_CACHE_DIR = os.environ.get("MODEL_CACHE_DIR", "/models")
ALIGN_MODEL_CACHE_DIR = os.environ.get("ALIGN_MODEL_CACHE_DIR", "/models/alignment")
ENGINE_VERSION = "whisperx-aligned-word-v1"

Path(MODEL_CACHE_DIR).mkdir(parents=True, exist_ok=True)
Path(ALIGN_MODEL_CACHE_DIR).mkdir(parents=True, exist_ok=True)
MODEL = whisperx.load_model(
    MODEL_NAME,
    DEVICE,
    compute_type=COMPUTE_TYPE,
    download_root=MODEL_CACHE_DIR,
    vad_method="silero",
)
_MODEL_LOCK = threading.Lock()
_ALIGN_MODELS: dict[str, tuple[Any, dict]] = {}


def _alignment_model(language: str) -> tuple[Any, dict]:
    cached = _ALIGN_MODELS.get(language)
    if cached is None:
        cached = whisperx.load_align_model(
            language_code=language,
            device=DEVICE,
            model_dir=ALIGN_MODEL_CACHE_DIR,
        )
        _ALIGN_MODELS[language] = cached
    return cached


def _transcribe_and_align(
    audio_path: Path, request, report: Callable[[int, str], None]
) -> tuple[dict, float]:
    audio = whisperx.load_audio(str(audio_path))
    duration = len(audio) / SAMPLE_RATE
    original_options = MODEL.options
    MODEL.options = replace(
        original_options,
        initial_prompt=request.initial_prompt,
        hotwords=request.hotwords,
    )
    try:
        transcription = MODEL.transcribe(
            audio,
            batch_size=BATCH_SIZE,
            language=request.language,
            task="transcribe",
            progress_callback=lambda progress: report(
                min(70, 15 + round(float(progress) * 0.55)),
                "WhisperX가 음성을 전사하는 중입니다.",
            ),
        )
    finally:
        MODEL.options = original_options

    language = str(transcription.get("language") or request.language or "").strip()
    if not language:
        raise RuntimeError("WhisperX가 전사 언어를 확인하지 못했습니다.")
    report(72, "WhisperX가 전사문을 원본 음성에 강제 정렬하는 중입니다.")
    align_model, align_metadata = _alignment_model(language)
    aligned = whisperx.align(
        transcription["segments"],
        align_model,
        align_metadata,
        audio,
        DEVICE,
        return_char_alignments=False,
        progress_callback=lambda progress: report(
            min(95, 72 + round(float(progress) * 0.23)),
            "WhisperX가 전사문을 원본 음성에 강제 정렬하는 중입니다.",
        ),
    )
    return {"language": language, **aligned}, duration


def transcribe(
    value: Any, progress_callback: Callable[[int, str], None] | None = None
) -> dict[str, Any]:
    def report(progress: int, message: str) -> None:
        if progress_callback:
            progress_callback(progress, message)

    try:
        request = parse_request(value)
        with tempfile.TemporaryDirectory(prefix="ave-whisperx-") as directory:
            report(5, "전사용 오디오를 다운로드하는 중입니다.")
            audio_path = download_audio(
                request.audio_url, Path(directory), progress_callback=report
            )
            if request.speed != 1.0:
                report(10, "전사 배속을 적용하는 중입니다.")
                audio_path = apply_speed(audio_path, request.speed)

            with _MODEL_LOCK:
                aligned, duration = _transcribe_and_align(audio_path, request, report)
            timestamped_segments = [
                {
                    "start": segment["start"],
                    "end": segment["end"],
                    "text": segment["text"],
                    "words": [
                        {
                            "start": word["start"],
                            "end": word["end"],
                            "word": word["word"],
                        }
                        for word in segment.get("words") or []
                        if isinstance(word, dict)
                        and isinstance(word.get("start"), (int, float))
                        and isinstance(word.get("end"), (int, float))
                        and str(word.get("word") or "").strip()
                    ],
                }
                for segment in aligned.get("segments") or []
                if isinstance(segment, dict)
                and isinstance(segment.get("start"), (int, float))
                and isinstance(segment.get("end"), (int, float))
                and str(segment.get("text") or "").strip()
            ]
            result_segments, original_duration = restore_original_segment_timestamps(
                timestamped_segments, duration, request.speed
            )
            for result_segment, source_segment in zip(
                result_segments, timestamped_segments, strict=True
            ):
                result_segment["words"] = [
                    {
                        "start": round(float(word["start"]) * request.speed, 3),
                        "end": round(float(word["end"]) * request.speed, 3),
                        "word": str(word["word"]),
                    }
                    for word in source_segment["words"]
                ]
            report(100, "WhisperX 전사와 강제 정렬이 완료되었습니다.")
            return {
                "text": "".join(segment["text"] for segment in result_segments).strip(),
                "language": aligned["language"],
                "duration": original_duration,
                "segments": result_segments,
                "engine": ENGINE_VERSION,
                "alignment": "ctc-forced-alignment-with-words",
            }
    except (RequestValidationError, AudioDownloadError) as error:
        return {"error": {"code": error.code, "message": str(error)}}
    except Exception:
        return {
            "error": {
                "code": "TRANSCRIPTION_FAILED",
                "message": "음성 전사 또는 강제 정렬에 실패했습니다.",
            }
        }

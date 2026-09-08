"""WhisperX 전환 전 faster-whisper 전사 엔진 보존본."""

import os
import tempfile
from pathlib import Path
from collections.abc import Callable
from typing import Any

from faster_whisper import WhisperModel

from src.input_audio import AudioDownloadError, apply_speed, download_audio
from src.request import RequestValidationError, parse_request
from src.timestamps import restore_original_segment_timestamps


MODEL_NAME = "large-v3"
COMPUTE_TYPE = "float16"
BEAM_SIZE = 5
VAD_PARAMETERS = {"min_silence_duration_ms": 500}


def _load_model() -> WhisperModel:
    cache_dir = os.environ.get("MODEL_CACHE_DIR", "/models")
    Path(cache_dir).mkdir(parents=True, exist_ok=True)
    return WhisperModel(MODEL_NAME, device="cuda", compute_type=COMPUTE_TYPE, download_root=cache_dir)


MODEL = _load_model()


def transcribe(value: Any, progress_callback: Callable[[int, str], None] | None = None) -> dict[str, Any]:
    def report(progress: int, message: str) -> None:
        if progress_callback:
            progress_callback(progress, message)

    try:
        request = parse_request(value)
        with tempfile.TemporaryDirectory(prefix="ave-whisper-") as directory:
            report(5, "전사용 오디오를 다운로드하는 중입니다.")
            audio_path = download_audio(request.audio_url, Path(directory), progress_callback=report)
            if request.speed != 1.0:
                report(10, "전사 배속을 적용하는 중입니다.")
                audio_path = apply_speed(audio_path, request.speed)
            segments, info = MODEL.transcribe(
                str(audio_path), task="transcribe", language=request.language,
                beam_size=BEAM_SIZE, vad_filter=True, vad_parameters=VAD_PARAMETERS,
                initial_prompt=request.initial_prompt, hotwords=request.hotwords,
                word_timestamps=False,
            )
            timestamped = [
                {"start": segment.start, "end": segment.end, "text": segment.text}
                for segment in segments if str(getattr(segment, "text", "")).strip()
            ]
            result, duration = restore_original_segment_timestamps(timestamped, info.duration, request.speed)
            return {
                "text": "".join(segment["text"] for segment in result).strip(),
                "language": info.language, "duration": duration, "segments": result,
            }
    except (RequestValidationError, AudioDownloadError) as error:
        return {"error": {"code": error.code, "message": str(error)}}
    except Exception:
        return {"error": {"code": "TRANSCRIPTION_FAILED", "message": "음성 전사에 실패했습니다."}}

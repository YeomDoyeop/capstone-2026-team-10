import os
import tempfile
from pathlib import Path
from collections.abc import Callable
from typing import Any

from faster_whisper import WhisperModel

from .input_audio import AudioDownloadError, apply_speed, download_audio
from .request import RequestValidationError, parse_request
from .timestamps import restore_original_timestamps


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

            report(15, "Whisper가 음성을 전사하는 중입니다.")
            segments, info = MODEL.transcribe(
                str(audio_path),
                task="transcribe",
                language=request.language,
                beam_size=BEAM_SIZE,
                vad_filter=True,
                vad_parameters=VAD_PARAMETERS,
                initial_prompt=request.initial_prompt,
                hotwords=request.hotwords,
            )
            result_segments = []
            duration = max(info.duration, 0.001)
            for segment in segments:
                result_segments.append({"start": round(segment.start, 3), "end": round(segment.end, 3), "text": segment.text.strip()})
                report(min(95, max(15, round(15 + 80 * segment.end / duration))), "Whisper가 음성을 전사하는 중입니다.")
            result_segments, duration = restore_original_timestamps(result_segments, info.duration, request.speed)
            report(100, "Whisper 전사가 완료되었습니다.")
            return {
                "text": "".join(segment["text"] for segment in result_segments).strip(),
                "language": info.language,
                "duration": duration,
                "segments": result_segments,
            }
    except (RequestValidationError, AudioDownloadError) as error:
        return {"error": {"code": error.code, "message": str(error)}}
    except Exception:
        # 세부 예외는 각 GPU 제공업체의 실행 로그에서 확인한다.
        return {"error": {"code": "TRANSCRIPTION_FAILED", "message": "음성 전사에 실패했습니다."}}

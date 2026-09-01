from dataclasses import dataclass
from typing import Any


class RequestValidationError(ValueError):
    code = "INVALID_INPUT"


@dataclass(frozen=True)
class TranscriptionRequest:
    audio_url: str
    language: str
    initial_prompt: str | None
    hotwords: str | None
    speed: float


def parse_request(value: Any) -> TranscriptionRequest:
    if not isinstance(value, dict):
        raise RequestValidationError("input 객체가 필요합니다.")

    audio_url = value.get("audio_url")
    if not isinstance(audio_url, str) or not audio_url.strip():
        raise RequestValidationError("audio_url은 비어 있지 않은 문자열이어야 합니다.")

    language = value.get("language", "ko")
    if not isinstance(language, str) or not language.strip():
        raise RequestValidationError("language는 비어 있지 않은 문자열이어야 합니다.")

    initial_prompt = _optional_text(value, "initial_prompt")
    hotwords = _optional_text(value, "hotwords")
    speed = value.get("speed", 1.0)
    if isinstance(speed, bool) or not isinstance(speed, (int, float)) or not 1.0 <= speed <= 4.0:
        raise RequestValidationError("speed는 1.0 이상 4.0 이하의 숫자여야 합니다.")

    return TranscriptionRequest(audio_url.strip(), language.strip(), initial_prompt, hotwords, float(speed))


def _optional_text(value: dict[str, Any], key: str) -> str | None:
    item = value.get(key)
    if item is None:
        return None
    if not isinstance(item, str):
        raise RequestValidationError(f"{key}는 문자열이어야 합니다.")
    return item.strip() or None

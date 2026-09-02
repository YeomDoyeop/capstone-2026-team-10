"""RunPod Queue에 배포된 ave-whisper-api 호출."""

from __future__ import annotations

import time
from typing import Any

import requests

from app.config import (
    get_runpod_api_key,
    get_whisper_runpod_endpoint_id,
    get_whisper_runpod_timeout_seconds,
)


class WhisperAPIError(RuntimeError):
    pass


def transcribe_with_whisper_api(audio_url: str, *, language: str, initial_prompt: str | None, hotwords: str | None, speed: float) -> dict[str, Any]:
    endpoint_id = get_whisper_runpod_endpoint_id()
    api_key = get_runpod_api_key()
    if not endpoint_id or not api_key:
        raise WhisperAPIError("WHISPER_RUNPOD_ENDPOINT_ID와 RUNPOD_API_KEY를 서버 .env에 설정하세요.")
    endpoint = f"https://api.runpod.ai/v2/{endpoint_id}"
    headers = {"Authorization": f"Bearer {api_key}"}
    payload = {"input": {"audio_url": audio_url, "language": language, "initial_prompt": initial_prompt, "hotwords": hotwords, "speed": speed}}
    try:
        response = requests.post(f"{endpoint}/run", json=payload, headers=headers, timeout=30)
        response.raise_for_status()
        runpod_job_id = response.json().get("id")
    except (requests.RequestException, ValueError, AttributeError) as exc:
        raise WhisperAPIError("Whisper API 작업 요청에 실패했습니다.") from exc
    if not isinstance(runpod_job_id, str) or not runpod_job_id:
        raise WhisperAPIError("Whisper API가 작업 ID를 반환하지 않았습니다.")
    deadline = time.monotonic() + get_whisper_runpod_timeout_seconds()
    while time.monotonic() < deadline:
        try:
            response = requests.get(f"{endpoint}/status/{runpod_job_id}", headers=headers, timeout=30)
            response.raise_for_status()
            status = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise WhisperAPIError("Whisper API 작업 상태를 조회하지 못했습니다.") from exc
        state = str(status.get("status", "")).upper()
        if state == "COMPLETED":
            output = status.get("output")
            if not isinstance(output, dict) or not isinstance(output.get("segments"), list):
                raise WhisperAPIError("Whisper API 응답 형식이 올바르지 않습니다.")
            error = output.get("error")
            if isinstance(error, dict):
                raise WhisperAPIError(str(error.get("message") or "Whisper API 전사에 실패했습니다."))
            return output
        if state in {"FAILED", "CANCELLED", "TIMED_OUT"}:
            raise WhisperAPIError(str(status.get("error") or "Whisper API 전사에 실패했습니다."))
        time.sleep(2)
    raise WhisperAPIError("Whisper API 전사 작업 시간이 초과되었습니다.")

"""서버 전용 LLM API 호출."""

from __future__ import annotations

import os
import threading
import time
from typing import Any

import requests


GEMINI_REQUESTS_PER_MINUTE = 4_000
_gemini_request_lock = threading.Lock()
_next_gemini_request_at = 0.0


def _wait_for_gemini_request_slot() -> None:
    """한 서버 프로세스의 모든 Gemini 요청 시작을 프로젝트 RPM 안에 맞춘다."""
    global _next_gemini_request_at
    interval = 60.0 / GEMINI_REQUESTS_PER_MINUTE
    with _gemini_request_lock:
        now = time.monotonic()
        scheduled = max(now, _next_gemini_request_at)
        if scheduled > now:
            time.sleep(scheduled - now)
        _next_gemini_request_at = scheduled + interval


class LLMGatewayError(RuntimeError):
    def __init__(self, message: str, *, unavailable: bool = False):
        super().__init__(message)
        self.unavailable = unavailable


def _provider_error(response: requests.Response, provider: str, api_key: str) -> str:
    """공급자 오류의 상태와 설명만 전달하고 인증 정보는 노출하지 않는다."""
    try:
        payload = response.json()
        error = payload.get("error") if isinstance(payload, dict) else None
        detail = error.get("message") if isinstance(error, dict) else None
    except (ValueError, TypeError):
        detail = None
    message = " ".join(detail.split()) if isinstance(detail, str) else ""
    if api_key:
        message = message.replace(api_key, "[비공개]")
    message = message[:500]
    suffix = f": {message}" if message else ""
    return f"{provider.upper()} API 오류 (HTTP {response.status_code}){suffix}"


def _post_provider(provider: str, url: str, **kwargs: Any) -> requests.Response:
    try:
        return requests.post(url, **kwargs)
    except requests.RequestException as exc:
        raise LLMGatewayError(
            f"{provider.upper()} API 네트워크 오류 ({type(exc).__name__})"
        ) from exc


def generate_json(
    provider: str,
    system: str,
    prompt: str,
    *,
    model: str | None,
    response_schema: dict[str, Any] | None,
) -> str:
    provider = provider.lower()
    api_key = os.getenv(f"{provider.upper()}_API_KEY", "").strip()
    if not api_key:
        raise LLMGatewayError(f"{provider.upper()}_API_KEY를 서버 .env에 설정하세요.")
    if provider == "gemini":
        _wait_for_gemini_request_slot()
        selected_model = model or os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
        config: dict[str, Any] = {
            "temperature": 0.1,
            "maxOutputTokens": 8192,
            "responseMimeType": "application/json",
        }
        if response_schema:
            # responseSchema는 제한된 OpenAPI Schema 형식이라 JSON Schema의
            # additionalProperties 등을 거부한다. 파일 계약은 JSON Schema이므로
            # REST API의 responseJsonSchema 필드로 그대로 전달한다.
            config["responseJsonSchema"] = response_schema
        response = _post_provider(
            provider,
            f"https://generativelanguage.googleapis.com/v1beta/models/{selected_model}:generateContent",
            headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
            json={
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": config,
            },
            timeout=120,
        )
    elif provider == "deepseek":
        selected_model = model or os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
        response = _post_provider(
            provider,
            "https://api.deepseek.com/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": selected_model,
                "messages": [
                    {
                        "role": "system",
                        "content": system
                        + "\n설명이나 코드펜스 없이 유효한 JSON만 반환하세요.",
                    },
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.1,
                "response_format": {"type": "json_object"},
                "max_tokens": 8192,
                "stream": False,
            },
            timeout=120,
        )
    else:
        raise LLMGatewayError("지원하지 않는 LLM 공급자입니다.")
    if response.status_code >= 400:
        raise LLMGatewayError(
            _provider_error(response, provider, api_key),
            unavailable=response.status_code in {429, 503},
        )
    try:
        response.raise_for_status()
        return (
            response.json()["candidates"][0]["content"]["parts"][0]["text"]
            if provider == "gemini"
            else response.json()["choices"][0]["message"]["content"]
        )
    except (
        requests.RequestException,
        KeyError,
        IndexError,
        TypeError,
        ValueError,
    ) as exc:
        raise LLMGatewayError("LLM API 호출에 실패했습니다.") from exc

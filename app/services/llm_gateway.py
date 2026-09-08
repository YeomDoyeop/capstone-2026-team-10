"""서버 전용 LLM API 호출."""

from __future__ import annotations

import os
from typing import Any

import requests


class LLMGatewayError(RuntimeError):
    def __init__(self, message: str, *, unavailable: bool = False):
        super().__init__(message)
        self.unavailable = unavailable


def generate_json(provider: str, system: str, prompt: str, *, model: str | None, response_schema: dict[str, Any] | None) -> str:
    provider = provider.lower()
    api_key = os.getenv(f"{provider.upper()}_API_KEY", "").strip()
    if not api_key:
        raise LLMGatewayError(f"{provider.upper()}_API_KEY를 서버 .env에 설정하세요.")
    if provider == "gemini":
        selected_model = model or os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
        config: dict[str, Any] = {"temperature": 0.1, "maxOutputTokens": 8192, "responseMimeType": "application/json"}
        if response_schema:
            config["responseSchema"] = response_schema
        response = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{selected_model}:generateContent", headers={"x-goog-api-key": api_key, "Content-Type": "application/json"}, json={"systemInstruction": {"parts": [{"text": system}]}, "contents": [{"role": "user", "parts": [{"text": prompt}]}], "generationConfig": config}, timeout=120)
    elif provider == "deepseek":
        selected_model = model or os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
        response = requests.post("https://api.deepseek.com/chat/completions", headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, json={"model": selected_model, "messages": [{"role": "system", "content": system + "\n설명이나 코드펜스 없이 유효한 JSON만 반환하세요."}, {"role": "user", "content": prompt}], "temperature": 0.1, "response_format": {"type": "json_object"}, "max_tokens": 8192, "stream": False}, timeout=120)
    else:
        raise LLMGatewayError("지원하지 않는 LLM 공급자입니다.")
    if response.status_code == 429:
        raise LLMGatewayError(
            f"{provider.upper()} API 요청 한도에 도달했습니다(HTTP 429). 무료 사용량 한도를 확인한 뒤 잠시 후 다시 시도하세요.",
            unavailable=True,
        )
    if response.status_code == 503:
        raise LLMGatewayError(
            f"{provider.upper()} API를 현재 사용할 수 없습니다(HTTP 503). 무료 사용량 한도 또는 공급자 일시 장애일 수 있습니다.",
            unavailable=True,
        )
    try:
        response.raise_for_status()
        return response.json()["candidates"][0]["content"]["parts"][0]["text"] if provider == "gemini" else response.json()["choices"][0]["message"]["content"]
    except (requests.RequestException, KeyError, IndexError, TypeError, ValueError) as exc:
        raise LLMGatewayError("LLM API 호출에 실패했습니다.") from exc

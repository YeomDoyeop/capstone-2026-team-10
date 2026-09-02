"""서버 전용 LLM API 호출."""

from __future__ import annotations

import os
from typing import Any

import requests


class LLMGatewayError(RuntimeError):
    pass


def generate_json(provider: str, system: str, prompt: str, *, model: str | None, response_schema: dict[str, Any] | None) -> str:
    provider = provider.lower()
    api_key = os.getenv(f"{provider.upper()}_API_KEY", "").strip()
    if not api_key:
        raise LLMGatewayError(f"{provider.upper()}_API_KEY를 서버 .env에 설정하세요.")
    if provider == "gemini":
        selected_model = model or os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
        config: dict[str, Any] = {"temperature": 0.15, "maxOutputTokens": 8192, "responseMimeType": "application/json"}
        if response_schema:
            config["responseSchema"] = response_schema
        response = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{selected_model}:generateContent", headers={"x-goog-api-key": api_key, "Content-Type": "application/json"}, json={"systemInstruction": {"parts": [{"text": system}]}, "contents": [{"role": "user", "parts": [{"text": prompt}]}], "generationConfig": config}, timeout=120)
    elif provider == "deepseek":
        selected_model = model or os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
        response = requests.post("https://api.deepseek.com/chat/completions", headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, json={"model": selected_model, "messages": [{"role": "system", "content": system + "\n설명이나 코드펜스 없이 유효한 JSON만 반환하세요."}, {"role": "user", "content": prompt}], "temperature": 0.15, "response_format": {"type": "json_object"}, "max_tokens": 8192, "stream": False}, timeout=120)
    else:
        raise LLMGatewayError("지원하지 않는 LLM 공급자입니다.")
    try:
        response.raise_for_status()
        return response.json()["candidates"][0]["content"]["parts"][0]["text"] if provider == "gemini" else response.json()["choices"][0]["message"]["content"]
    except (requests.RequestException, KeyError, IndexError, TypeError, ValueError) as exc:
        raise LLMGatewayError("LLM API 호출에 실패했습니다.") from exc

"""서버 전용 LLM API 호출."""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import requests

logger = logging.getLogger(__name__)


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
    def __init__(self, message: str, *, unavailable: bool = False,
                 code: str = "configuration_error", retryable: bool = False,
                 status_code: int = 502, upstream_status: int | None = None,
                 retry_after: int | None = None):
        super().__init__(f"[{code}] {message}")
        self.unavailable = unavailable
        self.code = code
        self.retryable = retryable
        self.status_code = status_code
        self.upstream_status = upstream_status
        self.retry_after = retry_after


def _retry_after(response, payload: Any) -> int | None:
    value = getattr(response, "headers", {}).get("Retry-After")
    if not value and isinstance(payload, dict):
        error = payload.get("error")
        details = error.get("details", []) if isinstance(error, dict) else []
        if isinstance(details, list):
            for item in details:
                if isinstance(item, dict) and str(item.get("@type", "")).endswith("google.rpc.RetryInfo"):
                    value = str(item.get("retryDelay", "")).removesuffix("s")
                    break
    if not value:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            date = parsedate_to_datetime(str(value))
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            seconds = (date - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    return math.ceil(max(0, seconds)) if math.isfinite(seconds) else None


def _upstream_error(provider: str, response) -> LLMGatewayError:
    status = response.status_code
    try:
        payload = response.json()
    except ValueError:
        payload = None
    # 공급자 원문에는 키·입력 스크립트가 포함될 수 있어 분류에만 사용한다.
    detail = json.dumps(payload, ensure_ascii=False).lower() if payload else ""
    code, hint = "upstream_http_error", "공급자 요청 처리에 실패했습니다."
    retryable = status in {408, 429, 500, 502, 503, 504}
    if status in {401, 403}:
        code, hint = "provider_auth", "서버의 API_KEY와 해당 모델 접근 권한을 확인하세요."
    elif status == 402 or re.search(r"insufficient.*(?:balance|quota)|billing|credit balance", detail):
        code, hint, retryable = "billing_required", "공급자 결제 설정과 잔액을 확인하세요.", False
    elif status == 404:
        code, hint = "model_not_found", "요청한 모델명과 공급자의 모델 지원 여부를 확인하세요."
    elif status == 429:
        if re.search(r"per.?day|daily", detail):
            code, hint, retryable = "daily_quota", "일일 API 한도를 소진했습니다. 한도 초기화 또는 요금제 확인이 필요합니다.", False
        else:
            code, hint = "rate_limit", "API 요청/토큰 한도에 도달했습니다. 공급자 사용량을 확인하세요."
    elif status in {400, 413, 422}:
        if re.search(r"context.*(?:length|limit)|too many tokens|token.*exceed|input.*(?:long|large)", detail) or status == 413:
            code, hint = "input_too_large", "입력이 공급자 한도를 초과했습니다. 분석 입력을 더 작게 분할해야 합니다."
        elif "schema" in detail:
            code, hint = "invalid_schema", "공급자가 출력 JSON 스키마를 거절했습니다. 서버의 스키마 전송 형식을 확인하세요."
        else:
            code, hint = "invalid_request", "공급자가 요청을 거절했습니다. 모델명·생성 옵션·입력 형식을 확인하세요."
    elif retryable:
        code, hint = "provider_unavailable", "공급자의 일시 장애 또는 처리 지연입니다."
    return LLMGatewayError(
        f"{provider.upper()} HTTP {status}: {hint}", code=code,
        retryable=retryable, upstream_status=status,
        status_code=status if status in {429, 503, 504} else 502,
        unavailable=status in {429, 503}, retry_after=_retry_after(response, payload),
    )


def _post(provider: str, url: str, **kwargs):
    try:
        response = requests.post(url, **kwargs)
    except requests.exceptions.SSLError as exc:
        raise LLMGatewayError("서버에서 공급자 HTTPS 인증서를 검증하지 못했습니다.", code="provider_tls") from exc
    except requests.Timeout as exc:
        raise LLMGatewayError(f"서버에서 공급자 응답을 기다리다 시간 초과했습니다({type(exc).__name__}).",
                              code="provider_timeout", retryable=True, status_code=504) from exc
    except requests.ConnectionError as exc:
        raise LLMGatewayError("서버에서 공급자에 연결하지 못했습니다.",
                              code="provider_connection", retryable=True, status_code=503) from exc
    except requests.RequestException as exc:
        raise LLMGatewayError("서버의 공급자 요청 전송에 실패했습니다.", code="provider_request") from exc
    if response.status_code >= 400:
        raise _upstream_error(provider, response)
    return response


def generate_json(
    provider: str,
    system: str,
    prompt: str,
    *,
    model: str | None,
    response_schema: dict[str, Any] | None,
    image: dict[str, str] | None = None,
) -> str:
    try:
        return _generate_json(provider, system, prompt, model=model, response_schema=response_schema, image=image)
    except LLMGatewayError as exc:
        # 원문 프롬프트, 응답, 키, 토큰 및 예외 traceback은 기록하지 않는다.
        logger.warning("LLM failure code=%s upstream_status=%s retryable=%s system_chars=%d prompt_chars=%d",
                       exc.code, exc.upstream_status, exc.retryable, len(system), len(prompt))
        raise


def _generate_json(provider: str, system: str, prompt: str, *, model: str | None,
                   response_schema: dict[str, Any] | None, image: dict[str, str] | None = None) -> str:
    provider = provider.lower()
    if provider not in {"gemini", "deepseek"}:
        raise LLMGatewayError("지원하지 않는 LLM 공급자입니다.")
    if image is not None and provider != "gemini":
        raise LLMGatewayError("이미지 분석은 Gemini만 지원합니다.")
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
            # 클라이언트는 JSON Schema를 전달한다(숫자 enum, additionalProperties 포함).
            config["responseJsonSchema"] = response_schema
        response = _post(
            provider,
            f"https://generativelanguage.googleapis.com/v1beta/models/{selected_model}:generateContent",
            headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
            json={
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": prompt}] + ([{
                    "inlineData": {"mimeType": image["mime_type"], "data": image["data"]}
                }] if image is not None else [])}],
                "generationConfig": config,
            },
            timeout=120,
        )
    elif provider == "deepseek":
        selected_model = model or os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
        response = _post(
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

    try:
        payload = response.json()
    except ValueError as exc:
        raise LLMGatewayError("공급자 응답 본문이 JSON이 아닙니다.", code="invalid_response", retryable=True) from exc
    try:
        if provider == "gemini":
            if payload.get("promptFeedback", {}).get("blockReason"):
                raise LLMGatewayError("공급자가 입력 분석을 차단했습니다.", code="content_blocked")
            candidate = payload["candidates"][0]
            finish = candidate.get("finishReason")
            if finish == "MAX_TOKENS":
                raise LLMGatewayError("출력 토큰 한도로 JSON 생성이 중단됐습니다. 분석 단위를 줄여야 합니다.", code="output_limit")
            if finish in {"SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII"}:
                raise LLMGatewayError("공급자가 응답 생성을 차단했습니다.", code="content_blocked")
            if finish not in {None, "STOP"}:
                raise LLMGatewayError("공급자가 JSON 생성을 정상 완료하지 못했습니다.", code="incomplete_response", retryable=True)
            parts = candidate["content"]["parts"]
            if not isinstance(parts, list):
                raise TypeError("parts must be a list")
            text = "".join(part["text"] for part in parts
                           if isinstance(part, dict) and not part.get("thought")
                           and isinstance(part.get("text"), str))
        else:
            choice = payload["choices"][0]
            if choice.get("finish_reason") == "length":
                raise LLMGatewayError("출력 토큰 한도로 JSON 생성이 중단됐습니다. 분석 단위를 줄여야 합니다.", code="output_limit")
            if choice.get("finish_reason") == "content_filter" or choice["message"].get("refusal"):
                raise LLMGatewayError("공급자가 응답 생성을 차단했습니다.", code="content_blocked")
            if choice.get("finish_reason") not in {None, "stop"}:
                raise LLMGatewayError("공급자가 JSON 생성을 정상 완료하지 못했습니다.", code="incomplete_response", retryable=True)
            text = choice["message"]["content"]
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        raise LLMGatewayError("공급자 응답에 필요한 텍스트 필드가 없습니다.", code="invalid_response", retryable=True) from exc
    if not isinstance(text, str) or not text.strip():
        raise LLMGatewayError("공급자가 빈 텍스트를 반환했습니다.", code="empty_response", retryable=True)
    return text

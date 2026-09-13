"""Provider-neutral LLM gateway used by the editing agents."""

from __future__ import annotations

from typing import Any
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests

from app.config import get_ave_server_url

SUPPORTED_LLM_PROVIDERS = ("gemini", "deepseek")

# 공급자별 한도는 공통 분석 계약과 분리한다. Gemini의 4,000 RPM 설정은
# AVE Server 한 프로세스에서 합산한다. 여러 서버·다른 API 사용자는 별도 관리한다.
LLM_PROVIDER_EXECUTION_LIMITS = {
    "deepseek": {"max_parallel_requests": 20, "minimum_request_interval_seconds": 0.05},
    "gemini": {
        "max_parallel_requests": 50,
        "minimum_request_interval_seconds": 0.015,
    },  # 작업당 최대 4,000 RPM
}


def safe_error_detail(value: Any) -> str:
    text = str(value)
    text = re.sub(r"https?://[^\s\"<>]+", "[서버 URL]", text)
    text = re.sub(r"(?i)Bearer\s+\S+", "Bearer [숨김]", text)
    text = re.sub(r"(?i)((?:api[_-]?key|token|authorization)\s*[\"']?\s*[:=]\s*[\"']?)[^\s\"',}]+", r"\1[숨김]", text)
    return re.sub(r"\s+", " ", text).strip()[:400]


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        delay = float(value)
        return max(0.0, delay) if 0 <= delay < float("inf") else None
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            return max(0.0, (date - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return None


class LLMGatewayError(RuntimeError):
    def __init__(self, message: str, *, unavailable: bool = False,
                 retryable: bool = False, status_code: int | None = None,
                 retry_after: float | None = None):
        super().__init__(message)
        self.unavailable = unavailable
        self.retryable = retryable
        self.status_code = status_code
        self.retry_after = retry_after


class LLMGateway:
    """Archive gateway contract, extended for this app's JSON-only agents."""

    def __init__(
        self,
        provider: str = "deepseek",
        *,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float = 150.0,
        server_access_token: str | None = None,
    ):
        self.provider = provider.lower().strip()
        if self.provider not in SUPPORTED_LLM_PROVIDERS:
            raise LLMGatewayError(f"지원하지 않는 LLM 공급자입니다: {provider}")
        self.server_url = get_ave_server_url()
        self.server_access_token = server_access_token or ""
        del api_key
        self.api_key = ""
        if not self.server_url.startswith("https://"):
            raise LLMGatewayError("AVE_SERVER_URL에 서버 HTTPS 주소를 설정하세요.")
        defaults = {"gemini": "gemini-3.5-flash-lite", "deepseek": "deepseek-chat"}
        self.model = (model or defaults[self.provider]).strip()
        self.timeout = timeout
        provider_limits = {"gemini": 24_000, "deepseek": 12_000}
        self.max_input_chars = provider_limits[self.provider]
        execution_limits = LLM_PROVIDER_EXECUTION_LIMITS[self.provider]
        self.max_parallel_requests = int(execution_limits["max_parallel_requests"])
        self.minimum_request_interval_seconds = float(
            execution_limits["minimum_request_interval_seconds"]
        )

    def request_json(
        self, system: str, prompt: str, *, response_schema: dict[str, Any] | None = None
    ) -> str:
        if self.server_url.startswith("https://"):
            if not self.server_access_token:
                raise LLMGatewayError("서버 LLM 호출에는 로그인 토큰이 필요합니다.")
            try:
                response = requests.post(
                    f"{self.server_url}/api/llm/generate",
                    headers={
                        "Authorization": self.server_access_token,
                        "Content-Type": "application/json",
                    },
                    json={
                        "provider": self.provider,
                        "model": self.model,
                        "system": system,
                        "prompt": prompt,
                        "response_schema": response_schema,
                    },
                    timeout=self.timeout,
                )
                status = getattr(response, "status_code", 200)
                if status >= 400:
                    try:
                        payload = response.json()
                        detail = payload.get("detail") or payload.get("error") or "서버 응답 오류"
                    except (ValueError, AttributeError):
                        detail = "서버 또는 프록시가 정상 JSON을 반환하지 않았습니다."
                    detail = safe_error_detail(detail)
                    permanent = bool(re.search(
                        r"(?i)per.?day|daily|일일|API_KEY|invalid.api.key|authentication|permission|billing|insufficient|invalid.model",
                        detail,
                    ))
                    headers = getattr(response, "headers", {})
                    retry_hint = headers.get("X-AVE-LLM-Retryable", "").lower()
                    if status == 502 and detail == "LLM API 호출에 실패했습니다." and not headers.get("X-AVE-LLM-Error-Code"):
                        detail += " 서버가 실제 원인을 전달하지 않았습니다. ave-server 업데이트/배포 상태와 서버 로그를 확인하세요."
                    retryable = status in {408, 429, 500, 502, 503, 504} and not permanent
                    if retry_hint == "false":
                        retryable = False
                    raise LLMGatewayError(
                        f"AVE 서버 LLM HTTP {status}: {detail} (공급자: {self.provider})",
                        unavailable=status in {429, 503},
                        retryable=retryable,
                        status_code=status,
                        retry_after=_retry_after_seconds(getattr(response, "headers", {}).get("Retry-After")),
                    )
                response.raise_for_status()
                value = response.json().get("text")
            except LLMGatewayError:
                raise
            except requests.exceptions.SSLError as exc:
                raise LLMGatewayError("AVE 서버 HTTPS 인증서 검증에 실패했습니다.") from exc
            except (requests.Timeout, requests.ConnectionError) as exc:
                raise LLMGatewayError(
                    f"AVE 서버 LLM 연결 오류({type(exc).__name__}).", retryable=True
                ) from exc
            except requests.RequestException as exc:
                raise LLMGatewayError(f"AVE 서버 LLM 요청 오류({type(exc).__name__}).") from exc
            except (ValueError, AttributeError) as exc:
                raise LLMGatewayError("AVE 서버 LLM 응답이 JSON 객체가 아닙니다.", retryable=True) from exc
            if not isinstance(value, str) or not value.strip():
                raise LLMGatewayError("AVE 서버 LLM 응답의 text가 비어 있거나 문자열이 아닙니다.", retryable=True)
            return value
        raise LLMGatewayError("LLM 공급자 직접 호출은 지원하지 않습니다.")

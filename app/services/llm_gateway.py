"""Provider-neutral LLM gateway used by the editing agents."""

from __future__ import annotations

from typing import Any

import requests

from app.config import get_ave_server_url

SUPPORTED_LLM_PROVIDERS = ("gemini", "deepseek")

# 공급자별 한도는 공통 분석 계약과 분리한다. 실제 계정 전체의 RPM 제한은
# AVE Server도 적용해야 하지만, 클라이언트는 한 작업 안에서 이를 넘지 않는다.
LLM_PROVIDER_EXECUTION_LIMITS = {
    "deepseek": {"max_parallel_requests": 100, "minimum_request_interval_seconds": 0.0},
    "gemini": {
        "max_parallel_requests": 1,
        "minimum_request_interval_seconds": 4.0,
    },  # 15 RPM
}


class LLMGatewayError(RuntimeError):
    def __init__(self, message: str, *, unavailable: bool = False):
        super().__init__(message)
        self.unavailable = unavailable


class LLMGateway:
    """Archive gateway contract, extended for this app's JSON-only agents."""

    def __init__(
        self,
        provider: str = "deepseek",
        *,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float = 120.0,
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
                if getattr(response, "status_code", None) in {429, 503}:
                    try:
                        detail = str(
                            response.json().get("detail")
                            or "LLM API 사용량 제한 또는 일시 장애"
                        )
                    except ValueError:
                        detail = "LLM API 사용량 제한 또는 일시 장애"
                    raise LLMGatewayError(detail, unavailable=True)
                response.raise_for_status()
                value = response.json().get("text")
            except LLMGatewayError:
                raise
            except (requests.RequestException, ValueError, AttributeError) as exc:
                raise LLMGatewayError("AVE 서버 LLM 호출에 실패했습니다.") from exc
            if not isinstance(value, str):
                raise LLMGatewayError("AVE 서버 LLM 응답 형식이 올바르지 않습니다.")
            return value
        raise LLMGatewayError("LLM 공급자 직접 호출은 지원하지 않습니다.")

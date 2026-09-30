from app.services import llm_gateway
import pytest
import requests


class _Response:
    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return {
            "candidates": [{"content": {"parts": [{"text": "{}"}]}}],
            "choices": [{"message": {"content": "{}"}}],
        }


def test_gemini_uses_stable_temperature(monkeypatch):
    sent = {}
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(
        llm_gateway.requests,
        "post",
        lambda *args, **kwargs: sent.update(kwargs) or _Response(),
    )

    llm_gateway.generate_json(
        "gemini", "system", "prompt", model=None, response_schema=None
    )

    assert sent["json"]["generationConfig"]["temperature"] == 0.1


def test_gemini_sends_json_schema_with_supported_response_field(monkeypatch):
    sent = {}
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"indexes": {"type": "array", "items": {"type": "integer"}}},
        "required": ["indexes"],
    }
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(
        llm_gateway.requests,
        "post",
        lambda *args, **kwargs: sent.update(kwargs) or _Response(),
    )

    llm_gateway.generate_json(
        "gemini", "system", "prompt", model=None, response_schema=schema
    )

    config = sent["json"]["generationConfig"]
    assert config["responseJsonSchema"] == schema
    assert "responseSchema" not in config


def test_gemini_omits_large_chapter_max_items_without_mutating_input(monkeypatch):
    sent = {}
    schema = {
        "type": "object",
        "properties": {"chapters": {"type": "array", "minItems": 1, "maxItems": 2339,
                                     "items": {"type": "object"}}},
    }
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(
        llm_gateway.requests, "post", lambda *args, **kwargs: sent.update(kwargs) or _Response()
    )

    llm_gateway.generate_json(
        "gemini", "system", "prompt", model=None, response_schema=schema
    )

    sent_chapters = sent["json"]["generationConfig"]["responseJsonSchema"]["properties"]["chapters"]
    assert sent_chapters["minItems"] == 1
    assert "maxItems" not in sent_chapters
    assert schema["properties"]["chapters"]["maxItems"] == 2339


def test_gemini_request_slots_enforce_four_thousand_rpm(monkeypatch):
    clock = [100.0]
    sleeps = []
    monkeypatch.setattr(llm_gateway, "_next_gemini_request_at", 0.0)
    monkeypatch.setattr(llm_gateway.time, "monotonic", lambda: clock[0])

    def advance(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(llm_gateway.time, "sleep", advance)

    llm_gateway._wait_for_gemini_request_slot()
    llm_gateway._wait_for_gemini_request_slot()
    llm_gateway._wait_for_gemini_request_slot()

    assert llm_gateway.GEMINI_REQUESTS_PER_MINUTE == 4_000
    assert sleeps == pytest.approx([0.015, 0.015])


def test_deepseek_uses_stable_temperature(monkeypatch):
    sent = {}
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setattr(
        llm_gateway.requests,
        "post",
        lambda *args, **kwargs: sent.update(kwargs) or _Response(),
    )

    llm_gateway.generate_json(
        "deepseek", "system", "prompt", model=None, response_schema=None
    )

    assert sent["json"]["temperature"] == 0.1


def test_gemini_error_exposes_status_and_message_without_api_key(monkeypatch):
    class ErrorResponse:
        status_code = 400

        def json(self):
            return {"error": {"message": "Invalid request with test-key"}}

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(llm_gateway.requests, "post", lambda *_a, **_k: ErrorResponse())

    with pytest.raises(llm_gateway.LLMGatewayError) as error:
        llm_gateway.generate_json(
            "gemini", "system", "prompt", model=None, response_schema=None
        )

    assert "GEMINI HTTP 400" in str(error.value)
    assert error.value.code == "invalid_request"
    assert "test-key" not in str(error.value)


def test_gemini_rate_limit_preserves_provider_detail(monkeypatch):
    class ErrorResponse:
        status_code = 429

        def json(self):
            return {"error": {"message": "Quota exceeded for model"}}

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(llm_gateway.requests, "post", lambda *_a, **_k: ErrorResponse())

    with pytest.raises(llm_gateway.LLMGatewayError) as error:
        llm_gateway.generate_json(
            "gemini", "system", "prompt", model=None, response_schema=None
        )

    assert error.value.unavailable is True
    assert error.value.code == "rate_limit"


def test_gemini_network_failure_identifies_failure_type(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    def timeout(*_args, **_kwargs):
        raise requests.ConnectTimeout("connection timed out")

    monkeypatch.setattr(llm_gateway.requests, "post", timeout)

    with pytest.raises(llm_gateway.LLMGatewayError, match="ConnectTimeout"):
        llm_gateway.generate_json(
            "gemini", "system", "prompt", model=None, response_schema=None
        )


class Response:
    def __init__(self, status=200, payload=None, headers=None):
        self.status_code = status
        self.payload = payload
        self.headers = headers or {}

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


def call(monkeypatch, response, provider="gemini", schema=None):
    monkeypatch.setenv(f"{provider.upper()}_API_KEY", "secret-api-key")
    monkeypatch.setattr(llm_gateway.requests, "post", lambda *a, **kw: response)
    return llm_gateway.generate_json(provider, "private-system", "private-script",
                                    model=None, response_schema=schema)


@pytest.mark.parametrize("status,payload,code,retryable", [
    (400, {"error": {"message": "invalid response schema"}}, "invalid_schema", False),
    (400, {"error": {"message": "context length exceeded"}}, "input_too_large", False),
    (400, {}, "invalid_request", False),
    (401, {}, "provider_auth", False),
    (402, {}, "billing_required", False),
    (403, {}, "provider_auth", False),
    (404, {}, "model_not_found", False),
    (413, {}, "input_too_large", False),
    (429, {"error": {"details": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}}, "daily_quota", False),
    (429, {}, "rate_limit", True),
    (500, {}, "provider_unavailable", True),
    (502, ValueError("not JSON"), "provider_unavailable", True),
    (503, {}, "provider_unavailable", True),
])
def test_http_errors_are_classified(monkeypatch, status, payload, code, retryable):
    with pytest.raises(llm_gateway.LLMGatewayError) as error:
        call(monkeypatch, Response(status, payload))
    assert error.value.code == code
    assert error.value.retryable is retryable
    assert error.value.upstream_status == status
    assert f"HTTP {status}" in str(error.value)


def test_provider_secrets_and_input_are_not_exposed(monkeypatch, caplog):
    payload = {"error": {"message": "secret-api-key private-script Bearer private-token"}}
    with pytest.raises(llm_gateway.LLMGatewayError) as error:
        call(monkeypatch, Response(400, payload))
    output = str(error.value) + caplog.text
    for secret in ("secret-api-key", "private-script", "private-token", "private-system"):
        assert secret not in output
    assert "code=invalid_request" in caplog.text


@pytest.mark.parametrize("exception,code,retryable", [
    (requests.Timeout("secret URL"), "provider_timeout", True),
    (requests.ConnectionError("secret URL"), "provider_connection", True),
    (requests.exceptions.SSLError("secret URL"), "provider_tls", False),
    (requests.RequestException("secret URL"), "provider_request", False),
])
def test_network_errors_are_wrapped(monkeypatch, exception, code, retryable):
    monkeypatch.setenv("GEMINI_API_KEY", "secret-api-key")
    def post(*a, **kw):
        raise exception
    monkeypatch.setattr(llm_gateway.requests, "post", post)
    with pytest.raises(llm_gateway.LLMGatewayError) as error:
        llm_gateway.generate_json("gemini", "system", "prompt", model=None, response_schema=None)
    assert error.value.code == code
    assert error.value.retryable is retryable
    assert "secret URL" not in str(error.value)


@pytest.mark.parametrize("header,delay", [("7", 7), ("2.5", 3), ("NaN", None), ("invalid", None)])
def test_retry_after_header_is_validated(monkeypatch, header, delay):
    with pytest.raises(llm_gateway.LLMGatewayError) as error:
        call(monkeypatch, Response(429, {}, {"Retry-After": header}))
    assert error.value.retry_after == delay


def test_retry_delay_body_is_forwarded(monkeypatch):
    payload = {"error": {"details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "59.4s"}]}}
    with pytest.raises(llm_gateway.LLMGatewayError) as error:
        call(monkeypatch, Response(429, payload))
    assert error.value.retry_after == 60


def test_gemini_uses_json_schema_field(monkeypatch):
    sent = {}
    schema = {"type": "object", "additionalProperties": False,
              "properties": {"id": {"type": "integer", "enum": [0, 1]}}}
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(llm_gateway.requests, "post", lambda *a, **kw: sent.update(kw) or _Response())
    llm_gateway.generate_json("gemini", "system", "prompt", model=None, response_schema=schema)
    config = sent["json"]["generationConfig"]
    assert config["responseJsonSchema"] == schema
    assert "responseSchema" not in config


def test_gemini_joins_text_parts_without_thoughts(monkeypatch):
    payload = {"candidates": [{"finishReason": "STOP", "content": {"parts": [
        {"thought": True, "text": "private reasoning"}, {"text": '{"a":'}, {"text": '1}'}
    ]}}]}
    assert call(monkeypatch, Response(payload=payload)) == '{"a":1}'


@pytest.mark.parametrize("payload,code,retryable", [
    (ValueError("invalid JSON"), "invalid_response", True),
    (None, "invalid_response", True),
    ({"candidates": []}, "invalid_response", True),
    ({"candidates": [{"content": {"parts": [{"text": ""}]}}]}, "empty_response", True),
    ({"candidates": [{"finishReason": "MAX_TOKENS"}]}, "output_limit", False),
    ({"candidates": [{"finishReason": "SAFETY"}]}, "content_blocked", False),
    ({"promptFeedback": {"blockReason": "SAFETY"}}, "content_blocked", False),
    ({"candidates": [{"finishReason": "OTHER"}]}, "incomplete_response", True),
])
def test_invalid_gemini_content_is_not_success(monkeypatch, payload, code, retryable):
    with pytest.raises(llm_gateway.LLMGatewayError) as error:
        call(monkeypatch, Response(payload=payload))
    assert error.value.code == code
    assert error.value.retryable is retryable


@pytest.mark.parametrize("choice,code", [
    ({"finish_reason": "length"}, "output_limit"),
    ({"finish_reason": "content_filter"}, "content_blocked"),
    ({"message": {"refusal": "blocked"}}, "content_blocked"),
    ({"message": {"content": None}}, "empty_response"),
    ({"message": {}}, "invalid_response"),
])
def test_invalid_deepseek_content_is_not_success(monkeypatch, choice, code):
    with pytest.raises(llm_gateway.LLMGatewayError) as error:
        call(monkeypatch, Response(payload={"choices": [choice]}), "deepseek")
    assert error.value.code == code


def test_deepseek_reads_only_final_content(monkeypatch):
    payload = {"choices": [{"finish_reason": "stop", "message": {
        "content": "{}", "reasoning_content": "private reasoning"}}]}
    assert call(monkeypatch, Response(payload=payload), "deepseek") == "{}"

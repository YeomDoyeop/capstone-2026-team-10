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

    assert "GEMINI API 오류 (HTTP 400)" in str(error.value)
    assert "Invalid request" in str(error.value)
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
    assert "Quota exceeded for model" in str(error.value)


def test_gemini_network_failure_identifies_failure_type(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    def timeout(*_args, **_kwargs):
        raise requests.ConnectTimeout("connection timed out")

    monkeypatch.setattr(llm_gateway.requests, "post", timeout)

    with pytest.raises(llm_gateway.LLMGatewayError, match="ConnectTimeout"):
        llm_gateway.generate_json(
            "gemini", "system", "prompt", model=None, response_schema=None
        )

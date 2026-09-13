from app.services.llm_gateway import LLMGateway


def test_each_provider_uses_its_lightweight_default_model(monkeypatch):
    monkeypatch.setenv("AVE_SERVER_URL", "https://ave-server.example.test")

    assert LLMGateway(api_key="test-key").provider == "deepseek"
    assert LLMGateway(api_key="test-key").model == "deepseek-chat"
    assert LLMGateway("gemini", api_key="test-key").model == "gemini-3.5-flash-lite"
    assert LLMGateway("deepseek", api_key="test-key").model == "deepseek-chat"


def test_provider_execution_limits_distinguish_deepseek_and_gemini(monkeypatch):
    monkeypatch.setenv("AVE_SERVER_URL", "https://ave-server.example.test")

    deepseek = LLMGateway("deepseek", api_key="test-key")
    gemini = LLMGateway("gemini", api_key="test-key")

    assert deepseek.max_parallel_requests == 100
    assert deepseek.minimum_request_interval_seconds == 0
    assert gemini.max_parallel_requests == 50
    assert gemini.minimum_request_interval_seconds == 0.015

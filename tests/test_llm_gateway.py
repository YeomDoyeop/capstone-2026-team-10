from app.services import llm_gateway


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

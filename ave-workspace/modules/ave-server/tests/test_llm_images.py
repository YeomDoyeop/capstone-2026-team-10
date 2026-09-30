import base64

import pytest
from fastapi.testclient import TestClient
from app.schemas import LLMGenerateRequest
from app.services import llm_gateway
from app.main import app, get_current_user

IMAGE = {"mime_type": "image/jpeg", "data": base64.b64encode(b"\xff\xd8\xfftest").decode()}


def test_image_contract_accepts_only_bounded_jpeg_and_gemini():
    LLMGenerateRequest(provider="gemini", system="s", prompt="p", image=IMAGE)
    for image, provider in [(IMAGE, "deepseek"), ({**IMAGE, "data": "not-base64"}, "gemini"),
                             ({**IMAGE, "data": base64.b64encode(b"text").decode()}, "gemini"),
                             ({**IMAGE, "data": base64.b64encode(b"\xff\xd8\xff" + b"x" * 2_000_000).decode()}, "gemini")]:
        with pytest.raises(ValueError):
            LLMGenerateRequest(provider=provider, system="s", prompt="p", image=image)


def test_gemini_gets_inline_image(monkeypatch):
    sent = {}
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    class Response:
        status_code = 200
        def json(self):
            return {"candidates": [{"content": {"parts": [{"text": "{}"}]}}]}
    monkeypatch.setattr(llm_gateway.requests, "post", lambda *a, **kw: sent.update(kw) or Response())
    llm_gateway.generate_json("gemini", "s", "p", model=None, response_schema=None, image=IMAGE)
    assert sent["json"]["contents"][0]["parts"][1]["inlineData"] == {"mimeType": "image/jpeg", "data": IMAGE["data"]}


def test_authenticated_route_acknowledges_image_forwarding(monkeypatch):
    sent = {}
    app.dependency_overrides[get_current_user] = lambda: {"id": "test"}
    monkeypatch.setattr("app.main.generate_json", lambda *a, **kw: sent.update(kw) or "{}")
    try:
        with TestClient(app) as client:
            result = client.post("/api/llm/generate", json={"provider": "gemini", "system": "s", "prompt": "p", "image": IMAGE})
        assert result.status_code == 200
        assert result.json()["image_used"] is True
        assert sent["image"] == IMAGE
    finally:
        app.dependency_overrides.clear()

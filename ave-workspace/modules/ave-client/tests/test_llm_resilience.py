import json
import threading

import pytest
import requests

from app.services.llm_analysis_service import LLMAnalysisError, LLMAnalysisService, _parse_json_object
from app.services.llm_gateway import LLMGateway, LLMGatewayError


class Response:
    def __init__(self, status=200, payload=None, headers=None):
        self.status_code = status
        self.payload = payload if payload is not None else {"text": "{}"}
        self.headers = headers or {}

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError("HTTP failure")


def agent_for(gateway):
    agent = object.__new__(LLMAnalysisService)
    agent.gateway = gateway
    agent._checkpoint_responses = {}
    agent._checkpoint_lock = threading.Lock()
    agent._retry_pause = lambda seconds, cancel: cancel() if cancel else None
    return agent


@pytest.mark.parametrize("status,retryable", [(401, False), (403, False), (400, False), (429, True), (502, True), (503, True), (504, True)])
def test_gateway_exposes_status_and_safe_detail(monkeypatch, status, retryable):
    monkeypatch.setenv("AVE_SERVER_URL", "https://example.test")
    monkeypatch.setattr("app.services.llm_gateway.requests.post", lambda *a, **kw: Response(
        status, {"detail": "timeout https://example.test/?key=secret123 token=secret456"}, {"Retry-After": "7"}))
    with pytest.raises(LLMGatewayError) as error:
        LLMGateway(server_access_token="Bearer test").request_json("system", "data")
    assert error.value.status_code == status
    assert error.value.retryable is retryable
    assert error.value.retry_after == 7
    assert "timeout" in str(error.value)
    assert "secret123" not in str(error.value) and "secret456" not in str(error.value)


def test_daily_quota_is_not_treated_as_transient(monkeypatch):
    monkeypatch.setenv("AVE_SERVER_URL", "https://example.test")
    monkeypatch.setattr("app.services.llm_gateway.requests.post", lambda *a, **kw: Response(
        503, {"detail": "quota GenerateRequestsPerDayPerProjectPerModel exceeded"}))
    with pytest.raises(LLMGatewayError) as error:
        LLMGateway(server_access_token="Bearer test").request_json("system", "data")
    assert error.value.retryable is False


def test_server_permanent_error_is_not_retried_even_with_502(monkeypatch):
    monkeypatch.setenv("AVE_SERVER_URL", "https://example.test")
    calls = []
    def post(*args, **kwargs):
        calls.append(kwargs)
        return Response(502, {"detail": "[output_limit] 출력 한도 초과"},
                        {"X-AVE-LLM-Retryable": "false"})
    monkeypatch.setattr("app.services.llm_gateway.requests.post", post)
    agent = agent_for(LLMGateway(server_access_token="Bearer test"))
    with pytest.raises(LLMAnalysisError, match="output_limit"):
        agent._request_json("역할", "입력")
    assert len(calls) == 1
    assert calls[0]["timeout"] > 120
    assert not agent._checkpoint_responses


def test_generic_server_error_explains_missing_diagnostics_and_provider(monkeypatch):
    monkeypatch.setenv("AVE_SERVER_URL", "https://example.test")
    monkeypatch.setattr("app.services.llm_gateway.requests.post", lambda *a, **kw: Response(
        502, {"detail": "LLM API 호출에 실패했습니다."}))
    with pytest.raises(LLMGatewayError) as error:
        LLMGateway("gemini", server_access_token="Bearer test").request_json("role", "input")
    assert "ave-server 업데이트/배포 상태" in str(error.value)
    assert "공급자: gemini" in str(error.value)
    assert error.value.retryable is True


def test_502_then_success_is_retried_and_checkpoints_only_success(monkeypatch):
    monkeypatch.setenv("AVE_SERVER_URL", "https://example.test")
    calls = []
    def post(*args, **kwargs):
        calls.append(kwargs)
        return Response(502, {"detail": "upstream unavailable"}) if len(calls) == 1 else Response(payload={"text": '{"ok":true}'})
    monkeypatch.setattr("app.services.llm_gateway.requests.post", post)
    agent = agent_for(LLMGateway(server_access_token="Bearer test"))
    assert agent._request_json("역할", "입력") == {"ok": True}
    assert agent._request_json("역할", "입력") == {"ok": True}
    assert len(calls) == 2
    assert len(agent._checkpoint_responses) == 1
    system = calls[0]["json"]["system"]
    assert "[공통 응답 계약]" in system and "[작업 지침]" in system and "[출력 스키마]" in system
    assert calls[0]["json"]["prompt"] == "입력"


@pytest.mark.parametrize("retryable,delay,expected", [(False, None, 1), (True, None, 4), (True, 120, 1)])
def test_transport_retries_are_bounded(retryable, delay, expected):
    class Gateway:
        calls = 0
        def request_json(self, *args, **kwargs):
            self.calls += 1
            raise LLMGatewayError("upstream", retryable=retryable, retry_after=delay)
    agent = agent_for(Gateway())
    with pytest.raises(LLMAnalysisError):
        agent._request_json("역할", "입력")
    assert agent.gateway.calls == expected
    assert not agent._checkpoint_responses


def test_retry_after_is_honored_and_wait_is_cancellable():
    class Gateway:
        calls = 0
        def request_json(self, *args, **kwargs):
            self.calls += 1
            raise LLMGatewayError("temporary", retryable=True, retry_after=12)
    agent = agent_for(Gateway())
    waits = []
    def stop_wait(seconds, cancel):
        waits.append(seconds)
        raise RuntimeError("cancelled")
    agent._retry_pause = stop_wait
    with pytest.raises(RuntimeError, match="cancelled"):
        agent._request_json("역할", "입력")
    assert waits == [12] and agent.gateway.calls == 1


def test_score_contract_failure_is_retried_not_cached():
    class Gateway:
        calls = 0
        def request_json(self, system, prompt, **kwargs):
            self.calls += 1
            assert kwargs["response_schema"]["properties"]["items"]["minItems"] == 2
            if self.calls == 1:
                return '{"items":[{"id":0,"score":800}]}'
            assert "직전 응답 거부 사유" in system
            return '{"items":[{"id":0,"score":800},{"id":1,"score":300}]}'
    agent = agent_for(Gateway())
    sections = [{"chapter_id": "one", "start": i, "text": str(i)} for i in range(2)]
    assert [s["llm_score"] for s in agent.score_sections(sections)] == [0.8, 0.3]
    assert agent.gateway.calls == 2
    agent.score_sections(sections)
    assert agent.gateway.calls == 2


def test_large_score_request_is_batched_without_dropping_sections():
    class Gateway:
        max_input_chars = 12000
        def __init__(self):
            self.batches = []
        def request_json(self, system, prompt, **kwargs):
            rows = json.loads(prompt)["sections"]
            self.batches.append(rows)
            return json.dumps({"items": [{"id": r["id"], "score": 500} for r in rows]})
    agent = agent_for(Gateway())
    sections = [{"chapter_id": "one", "start": i, "text": f"뉴스 {i}"} for i in range(70)]
    result = agent.score_sections(sections)
    assert [s["start"] for s in result] == list(range(70))
    assert sorted(len(batch) for batch in agent.gateway.batches) == [6, 32, 32]
    assert sum(len(batch) for batch in agent.gateway.batches) == 70


@pytest.mark.parametrize("text", ['{"a":1,"a":2}', '{"a":NaN}', '{"a":Infinity}', '[1,2]', '{"a":'])
def test_invalid_json_is_not_silently_repaired(text):
    with pytest.raises((LLMAnalysisError, ValueError)):
        _parse_json_object(text)


def test_complete_json_code_fence_is_unwrapped():
    assert _parse_json_object('```json\n{"a":1}\n```') == {"a": 1}


def test_request_spacing_also_applies_to_retries(monkeypatch):
    import app.services.llm_analysis_service as service_module

    class Clock:
        now = 100.0

        def monotonic(self):
            return self.now

        def sleep(self, seconds):
            self.now += max(seconds, 0.000001)

    clock = Clock()
    starts = []

    class Gateway:
        def request_json(self, *args, **kwargs):
            starts.append(clock.now)
            if len(starts) == 1:
                raise LLMGatewayError("temporary", retryable=True)
            return '{}'

    monkeypatch.setattr(service_module, "time", clock)
    agent = agent_for(Gateway())
    agent._minimum_request_interval_seconds = 0.05
    agent._request_limit_lock = threading.Lock()
    agent._last_request_started_at = 0.0
    # 서로 다른 입력은 캐시로 생략되지 않는다. 첫 입력에는 재시도 한 번이 있다.
    for index in range(24):
        assert agent._request_json("role", str(index)) == {}
    assert len(starts) == 25
    assert all(b - a >= 0.05 - 1e-8 for a, b in zip(starts, starts[1:]))
    assert starts[-1] - starts[0] >= 1.2 - 1e-8


def test_request_slot_wait_can_be_cancelled():
    agent = agent_for(object())
    agent._minimum_request_interval_seconds = 0.05
    agent._request_limit_lock = threading.Lock()
    agent._last_request_started_at = float("inf")

    def cancel():
        raise RuntimeError("cancelled")

    with pytest.raises(RuntimeError, match="cancelled"):
        agent._wait_for_request_slot(cancel)
    assert not agent._request_limit_lock.locked()

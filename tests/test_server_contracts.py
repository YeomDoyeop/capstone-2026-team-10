from __future__ import annotations

from app.services.llm_gateway import LLMGateway
import pytest

from app.services.server_job_service import _score, create_job, save_result


class FakeResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self.payload


def test_llm_gateway_uses_authenticated_ave_server_api(monkeypatch):
    monkeypatch.setenv("AVE_SERVER_URL", "https://ave-server.example.test")
    captured: dict = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return FakeResponse({"text": "{}"})

    monkeypatch.setattr("app.services.llm_gateway.requests.post", fake_post)

    response = LLMGateway("gemini", server_access_token="Bearer session").request_json(
        "system", "prompt", response_schema={"type": "object"}
    )

    assert response == "{}"
    assert captured["url"] == "https://ave-server.example.test/api/llm/generate"
    assert captured["headers"]["Authorization"] == "Bearer session"
    assert captured["json"]["provider"] == "gemini"


def test_job_result_sends_only_analysis_data_to_server(monkeypatch):
    monkeypatch.setenv("AVE_SERVER_URL", "https://ave-server.example.test")
    requests: list[dict] = []

    def fake_request(method, url, **kwargs):
        requests.append({"method": method, "url": url, **kwargs})
        return FakeResponse({"id": "server-job"})

    monkeypatch.setattr("app.services.server_job_service.requests.request", fake_request)

    job_id = create_job(
        "Bearer session",
        client_job_id="local-job",
        source_id="youtube-id",
        source_url="https://www.youtube.com/watch?v=youtube-id",
    )
    save_result(
        "Bearer session",
        job_id,
        {
            "summary": "분석 결과",
            "recommended_segment_ids": ["segment-1"],
            "candidates": [
                {
                    "segment_id": "segment-1",
                    "start": 1.5,
                    "end": 4.0,
                    "text": "중요 발언",
                    "llm_score": 900,
                    "final_score": 0.8,
                }
            ],
        },
    )

    assert requests[0]["url"].endswith("/api/analysis-jobs")
    result_request = requests[1]
    assert result_request["url"].endswith("/api/analysis-jobs/server-job/result")
    assert result_request["headers"]["Authorization"] == "Bearer session"
    assert result_request["json"]["script"] is None
    assert result_request["json"]["segments"][0]["start_ms"] == 1500
    assert result_request["json"]["segments"][0]["script_importance"] == 0.9
    assert result_request["json"]["segments"][0]["final_score"] == 0.9
    assert "video_path" not in result_request["json"]


def test_repeated_result_sync_reuses_same_server_job(monkeypatch):
    monkeypatch.setenv("AVE_SERVER_URL", "https://ave-server.example.test")
    requests: list[dict] = []

    def fake_request(method, url, **kwargs):
        requests.append({"method": method, "url": url, **kwargs})
        return FakeResponse({"id": "server-job-existing"})

    monkeypatch.setattr("app.services.server_job_service.requests.request", fake_request)
    result = {"candidates": []}
    for _ in range(2):
        job_id = create_job("Bearer session", client_job_id="local-job", source_id="youtube-id", source_url="https://www.youtube.com/watch?v=youtube-id")
        save_result("Bearer session", job_id, result)

    assert [request["method"] for request in requests] == ["POST", "PUT", "POST", "PUT"]
    assert [request["url"] for request in requests if request["method"] == "PUT"] == [
        "https://ave-server.example.test/api/analysis-jobs/server-job-existing/result",
        "https://ave-server.example.test/api/analysis-jobs/server-job-existing/result",
    ]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(0, 0.0), (500, 0.5), (1000, 1.0), (-5, 0.0), (1200, 1.0), ("bad", None)],
)
def test_server_score_normalizes_client_scale(raw, expected):
    assert _score(raw) == expected

from fastapi.testclient import TestClient

from app.main import app, get_current_user


def test_health_check_returns_ok():
    response = TestClient(app).get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_api_requires_bearer_token():
    response = TestClient(app).get("/api/auth/me")

    assert response.status_code == 401


def test_create_analysis_job_records_client_reference(monkeypatch):
    app.dependency_overrides[get_current_user] = lambda: {"id": "user-001", "email": "user@example.com"}
    monkeypatch.setattr("app.main.is_storage_configured", lambda: True)

    def fake_create_job(user_id, values):
        assert user_id == "user-001"
        assert values["client_job_id"] == "local-job-001"
        assert values["source_id"] == "video-001"
        assert values["status"] == "queued"
        return values

    monkeypatch.setattr("app.main.create_job", fake_create_job)
    response = TestClient(app).post(
        "/api/analysis-jobs",
        json={"client_job_id": "local-job-001", "source_id": "video-001", "duration_ms": 60000},
    )
    app.dependency_overrides.clear()

    assert response.status_code == 201
    assert response.json()["client_job_id"] == "local-job-001"
    assert response.json()["status"] == "queued"
    assert response.json()["progress"] == 0


def test_result_rejects_invalid_segment_time(monkeypatch):
    app.dependency_overrides[get_current_user] = lambda: {"id": "user-001"}
    monkeypatch.setattr("app.main.is_storage_configured", lambda: True)
    response = TestClient(app).put(
        "/api/analysis-jobs/job-001/result",
        json={
            "segments": [
                {"segment_index": 0, "start_ms": 1000, "end_ms": 1000},
            ]
        },
    )
    app.dependency_overrides.clear()

    assert response.status_code == 422

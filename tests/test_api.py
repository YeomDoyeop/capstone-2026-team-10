import asyncio
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from pathlib import Path

from app.main import _cancel_persisted_transcription, _lease_has_expired, app, get_current_user
from app.services.whisper_api_service import WhisperAPIError


def _mock_transcription_store(monkeypatch):
    jobs = {}
    monkeypatch.setattr("app.main.is_storage_configured", lambda: True)
    monkeypatch.setattr("app.main.get_expired_transcription_jobs", lambda _: [])
    monkeypatch.setattr("app.main.create_transcription_job", lambda user_id, values: jobs.setdefault(values["runpod_job_id"], {"user_id": user_id, **values}))
    monkeypatch.setattr("app.main.get_transcription_job", lambda user_id, job_id: jobs.get(job_id) if jobs.get(job_id, {}).get("user_id") == user_id else None)

    def update(user_id, job_id, values):
        job = jobs.get(job_id)
        if job is None or job["user_id"] != user_id:
            return None
        job.update(values)
        return job

    monkeypatch.setattr("app.main.update_transcription_job", update)
    return jobs


def test_health_check_returns_ok():
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_api_requires_bearer_token():
    with TestClient(app) as client:
        response = client.get("/api/auth/me")

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
    with TestClient(app) as client:
        response = client.post(
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
    with TestClient(app) as client:
        response = client.put(
            "/api/analysis-jobs/job-001/result",
            json={
                "segments": [
                    {"segment_index": 0, "start_ms": 1000, "end_ms": 1000},
                ]
            },
        )
    app.dependency_overrides.clear()

    assert response.status_code == 422


def test_transcription_status_returns_runpod_progress_and_result(monkeypatch, tmp_path):
    app.dependency_overrides[get_current_user] = lambda: {"id": "user-001"}
    _mock_transcription_store(monkeypatch)
    audio_path = Path(tmp_path) / "audio.mp3"
    audio_path.write_bytes(b"audio")
    monkeypatch.setattr("app.main._temporary_audio_path", lambda _: audio_path)
    monkeypatch.setattr("app.main.get_public_base_url", lambda: "https://server.example")
    monkeypatch.setattr("app.main.start_transcription_with_whisper_api", lambda *args, **kwargs: "runpod-001")
    monkeypatch.setattr(
        "app.main.get_transcription_status",
        lambda _: {"status": "IN_PROGRESS", "progress": {"progress": 67, "message": "Whisper가 음성을 전사하는 중입니다."}},
    )
    with TestClient(app) as client:
        started = client.post("/api/stt/transcriptions", json={"file_id": "a" * 32, "client_job_id": "local-001", "track_progress": True})
        progress = client.get("/api/stt/transcriptions/runpod-001")
        monkeypatch.setattr("app.main.get_transcription_status", lambda _: {"status": "COMPLETED", "output": {"segments": []}})
        completed = client.get("/api/stt/transcriptions/runpod-001")
    app.dependency_overrides.clear()
    assert started.status_code == 202
    assert progress.json()["progress"] == 67
    assert completed.json()["status"] == "completed"
    assert completed.json()["result"] == {"segments": []}
    assert not audio_path.exists()


def test_transcription_status_reads_progress_from_runpod_output(monkeypatch, tmp_path):
    app.dependency_overrides[get_current_user] = lambda: {"id": "user-001"}
    _mock_transcription_store(monkeypatch)
    audio_path = Path(tmp_path) / "audio.mp3"
    audio_path.write_bytes(b"audio")
    monkeypatch.setattr("app.main._temporary_audio_path", lambda _: audio_path)
    monkeypatch.setattr("app.main.get_public_base_url", lambda: "https://server.example")
    monkeypatch.setattr("app.main.start_transcription_with_whisper_api", lambda *args, **kwargs: "runpod-002")
    monkeypatch.setattr(
        "app.main.get_transcription_status",
        lambda _: {"status": "IN_PROGRESS", "output": {"progress": 67, "message": "Whisper가 음성을 전사하는 중입니다."}},
    )
    with TestClient(app) as client:
        client.post("/api/stt/transcriptions", json={"file_id": "a" * 32, "client_job_id": "local-002", "track_progress": True})
        response = client.get("/api/stt/transcriptions/runpod-002")
    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["progress"] == 67
    assert response.json()["message"] == "Whisper가 음성을 전사하는 중입니다."


def test_transcription_heartbeat_and_cancel_are_owned_and_idempotent(monkeypatch, tmp_path):
    app.dependency_overrides[get_current_user] = lambda: {"id": "user-001"}
    jobs = _mock_transcription_store(monkeypatch)
    audio_path = Path(tmp_path) / "audio.mp3"
    audio_path.write_bytes(b"audio")
    monkeypatch.setattr("app.main._temporary_audio_path", lambda _: audio_path)
    monkeypatch.setattr("app.main.get_public_base_url", lambda: "https://server.example")
    monkeypatch.setattr("app.main.start_transcription_with_whisper_api", lambda *args, **kwargs: "runpod-cancel")
    cancelled = []
    monkeypatch.setattr("app.main.cancel_transcription", lambda job_id: cancelled.append(job_id))
    with TestClient(app) as client:
        started = client.post("/api/stt/transcriptions", json={"file_id": "a" * 32, "client_job_id": "local-cancel", "track_progress": True})
        heartbeat = client.post("/api/stt/transcriptions/runpod-cancel/heartbeat")
        first = client.post("/api/stt/transcriptions/runpod-cancel/cancel")
        second = client.post("/api/stt/transcriptions/runpod-cancel/cancel")
    app.dependency_overrides.clear()

    assert started.status_code == 202
    assert heartbeat.status_code == 200
    assert heartbeat.json()["lease_expires_at"]
    assert first.json()["status"] == "cancelled"
    assert second.json()["status"] == "cancelled"
    assert cancelled == ["runpod-cancel"]
    assert jobs["runpod-cancel"]["status"] == "cancelled"
    assert not audio_path.exists()


def test_transcription_rejects_another_users_heartbeat_and_cancel(monkeypatch, tmp_path):
    current_user = {"id": "user-001"}
    app.dependency_overrides[get_current_user] = lambda: current_user
    _mock_transcription_store(monkeypatch)
    audio_path = Path(tmp_path) / "audio.mp3"
    audio_path.write_bytes(b"audio")
    monkeypatch.setattr("app.main._temporary_audio_path", lambda _: audio_path)
    monkeypatch.setattr("app.main.get_public_base_url", lambda: "https://server.example")
    monkeypatch.setattr("app.main.start_transcription_with_whisper_api", lambda *args, **kwargs: "runpod-owned")
    with TestClient(app) as client:
        started = client.post("/api/stt/transcriptions", json={"file_id": "a" * 32, "client_job_id": "local-owned", "track_progress": True})
        current_user["id"] = "user-002"
        heartbeat = client.post("/api/stt/transcriptions/runpod-owned/heartbeat")
        cancelled = client.post("/api/stt/transcriptions/runpod-owned/cancel")
    app.dependency_overrides.clear()

    assert started.status_code == 202
    assert heartbeat.status_code == 404
    assert cancelled.status_code == 404
    assert audio_path.exists()


def test_expired_lease_cancels_runpod_and_removes_temporary_audio(monkeypatch, tmp_path):
    jobs = _mock_transcription_store(monkeypatch)
    audio_path = Path(tmp_path) / "audio.mp3"
    audio_path.write_bytes(b"audio")
    job = jobs.setdefault(
        "runpod-expired",
        {
            "user_id": "user-001",
            "runpod_job_id": "runpod-expired",
            "file_id": "a" * 32,
            "status": "in_progress",
        },
    )
    monkeypatch.setattr("app.main._temporary_audio_path", lambda _: audio_path)
    cancelled = []
    monkeypatch.setattr("app.main.cancel_transcription", lambda job_id: cancelled.append(job_id))

    result = asyncio.run(_cancel_persisted_transcription(job, "lease 만료"))

    assert result["status"] == "cancelled"
    assert cancelled == ["runpod-expired"]
    assert jobs["runpod-expired"]["cancel_requested_at"]
    assert not audio_path.exists()


def test_cancel_race_with_completed_runpod_keeps_completed_state(monkeypatch, tmp_path):
    jobs = _mock_transcription_store(monkeypatch)
    audio_path = Path(tmp_path) / "audio.mp3"
    audio_path.write_bytes(b"audio")
    job = jobs.setdefault(
        "runpod-completed",
        {
            "user_id": "user-001",
            "runpod_job_id": "runpod-completed",
            "file_id": "a" * 32,
            "status": "in_progress",
        },
    )
    monkeypatch.setattr("app.main._temporary_audio_path", lambda _: audio_path)
    monkeypatch.setattr("app.main.cancel_transcription", lambda _: (_ for _ in ()).throw(WhisperAPIError("already terminal")))
    monkeypatch.setattr("app.main.get_transcription_status", lambda _: {"status": "COMPLETED"})

    result = asyncio.run(_cancel_persisted_transcription(job, "사용자 취소"))

    assert result["status"] == "completed"
    assert jobs["runpod-completed"]["completed_at"]
    assert not audio_path.exists()


def test_lease_expiration_is_detected_before_a_late_heartbeat():
    assert _lease_has_expired({"runpod_job_id": "expired", "lease_expires_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()})
    assert not _lease_has_expired({"runpod_job_id": "active", "lease_expires_at": (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()})

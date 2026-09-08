import asyncio
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from pathlib import Path
import pytest

from app.main import _cancel_persisted_transcription, _lease_has_expired, app, get_current_user
from app.services.whisper_api_service import WhisperAPIError


def _mock_transcription_store(monkeypatch):
    jobs = {}
    cancel_requests = set()
    monkeypatch.setattr("app.main.is_storage_configured", lambda: True)
    monkeypatch.setattr("app.main.get_expired_transcription_jobs", lambda _: [])
    monkeypatch.setattr("app.main.get_expired_transcription_results", lambda _: [])
    monkeypatch.setattr("app.main.create_transcription_job", lambda user_id, values: jobs.setdefault(values["runpod_job_id"], {"user_id": user_id, **values}))
    monkeypatch.setattr("app.main.get_transcription_job", lambda user_id, job_id: jobs.get(job_id) if jobs.get(job_id, {}).get("user_id") == user_id else None)
    monkeypatch.setattr(
        "app.main.get_active_transcription_job_by_client_id",
        lambda user_id, client_job_id: next(
            (
                job
                for job in jobs.values()
                if job["user_id"] == user_id
                and job.get("client_job_id") == client_job_id
                and job.get("status") in {"queued", "in_progress", "cancel_requested"}
            ),
            None,
        ),
    )
    monkeypatch.setattr("app.main.request_transcription_cancel", lambda user_id, client_job_id, expires_at: cancel_requests.add((user_id, client_job_id)))
    monkeypatch.setattr("app.main.is_transcription_cancel_requested", lambda user_id, client_job_id: (user_id, client_job_id) in cancel_requests)
    monkeypatch.setattr("app.main.clear_transcription_cancel_request", lambda user_id, client_job_id: cancel_requests.discard((user_id, client_job_id)))
    monkeypatch.setattr("app.main.clear_expired_transcription_cancel_requests", lambda _: None)
    monkeypatch.setattr("app.main.delete_transcription_job", lambda user_id, job_id: jobs.pop(job_id, None) if jobs.get(job_id, {}).get("user_id") == user_id else None)

    def update(user_id, job_id, values):
        job = jobs.get(job_id)
        if job is None or job["user_id"] != user_id:
            return None
        job.update(values)
        return job

    monkeypatch.setattr("app.main.update_transcription_job", update)
    return jobs, cancel_requests


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
    monkeypatch.setattr("app.main.get_job_by_client_id", lambda *_: None)

    def fake_create_job(user_id, values):
        assert user_id == "user-001"
        assert values["client_job_id"] == "local-job-001"
        assert values["source_id"] == "video-001"
        assert values["status"] == "completed"
        assert values["progress"] == 100
        assert values["completed_at"]
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
    assert response.json()["status"] == "completed"
    assert response.json()["progress"] == 100


def test_create_analysis_job_returns_existing_client_job(monkeypatch):
    app.dependency_overrides[get_current_user] = lambda: {"id": "user-001"}
    monkeypatch.setattr("app.main.is_storage_configured", lambda: True)
    existing = {"id": "server-job-001", "client_job_id": "local-job-001", "status": "completed", "progress": 100}
    monkeypatch.setattr("app.main.get_job_by_client_id", lambda user_id, client_job_id: existing)
    monkeypatch.setattr("app.main.create_job", lambda *_: pytest.fail("기존 작업에는 INSERT하면 안 됩니다."))
    with TestClient(app) as client:
        first = client.post("/api/analysis-jobs", json={"client_job_id": "local-job-001", "source_id": "video-001"})
        retry = client.post("/api/analysis-jobs", json={"client_job_id": "local-job-001", "source_id": "video-001"})
    app.dependency_overrides.clear()

    assert first.status_code == 201
    assert retry.status_code == 201
    assert first.json() == retry.json() == existing


def test_create_analysis_job_recovers_unique_conflict_from_concurrent_request(monkeypatch):
    app.dependency_overrides[get_current_user] = lambda: {"id": "user-001"}
    monkeypatch.setattr("app.main.is_storage_configured", lambda: True)
    existing = {"id": "server-job-race", "client_job_id": "local-job-race", "status": "completed", "progress": 100}
    lookups = iter([None, existing])
    monkeypatch.setattr("app.main.get_job_by_client_id", lambda *_: next(lookups))

    class UniqueViolation(Exception):
        code = "23505"

    monkeypatch.setattr("app.main.create_job", lambda *_: (_ for _ in ()).throw(UniqueViolation("duplicate key value violates unique constraint")))
    with TestClient(app) as client:
        response = client.post("/api/analysis-jobs", json={"client_job_id": "local-job-race", "source_id": "video-001"})
    app.dependency_overrides.clear()

    assert response.status_code == 201
    assert response.json() == existing


def test_save_analysis_result_retries_are_upserts(monkeypatch):
    app.dependency_overrides[get_current_user] = lambda: {"id": "user-001"}
    monkeypatch.setattr("app.main.is_storage_configured", lambda: True)
    saved = []

    def fake_upsert(user_id, job_id, values):
        saved.append((user_id, job_id, values))
        return {"job_id": job_id, **values}

    monkeypatch.setattr("app.main.upsert_result", fake_upsert)
    payload = {"segments": [{"segment_index": 0, "start_ms": 0, "end_ms": 1000}]}
    with TestClient(app) as client:
        first = client.put("/api/analysis-jobs/server-job-001/result", json=payload)
        retry = client.put("/api/analysis-jobs/server-job-001/result", json=payload)
    app.dependency_overrides.clear()

    assert first.status_code == 200
    assert retry.status_code == 200
    assert [item[1] for item in saved] == ["server-job-001", "server-job-001"]


def test_analysis_job_reports_service_key_error_without_schema_message(monkeypatch):
    app.dependency_overrides[get_current_user] = lambda: {"id": "user-001"}
    monkeypatch.setattr("app.main.is_storage_configured", lambda: True)
    monkeypatch.setattr("app.main.get_job_by_client_id", lambda *_: (_ for _ in ()).throw(RuntimeError("Invalid API key")))
    with TestClient(app) as client:
        response = client.post("/api/analysis-jobs", json={"client_job_id": "local-job-001", "source_id": "video-001"})
    app.dependency_overrides.clear()

    assert response.status_code == 503
    assert response.json()["detail"] == "AVE 서버 저장소 인증 설정을 확인하세요."


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


def test_completed_transcription_result_survives_reconnect_until_ack(monkeypatch, tmp_path):
    app.dependency_overrides[get_current_user] = lambda: {"id": "user-001"}
    jobs, _ = _mock_transcription_store(monkeypatch)
    audio_path = Path(tmp_path) / "audio.mp3"
    audio_path.write_bytes(b"audio")
    monkeypatch.setattr("app.main._temporary_audio_path", lambda _: audio_path)
    monkeypatch.setattr("app.main.get_public_base_url", lambda: "https://server.example")
    monkeypatch.setattr("app.main.start_transcription_with_whisper_api", lambda *args, **kwargs: "runpod-ack")
    monkeypatch.setattr(
        "app.main.get_transcription_status",
        lambda _: {"status": "COMPLETED", "output": {
            "segments": [{"start": 1, "end": 2, "text": " 테스트", "words": [
                {"start": 1, "end": 2, "word": " 테스트"},
            ]}],
            "engine": "whisperx-aligned-word-v1",
            "alignment": "ctc-forced-alignment-with-words",
        }},
    )
    with TestClient(app) as client:
        client.post("/api/stt/transcriptions", json={"file_id": "a" * 32, "client_job_id": "local-ack", "track_progress": True})
        completed = client.get("/api/stt/transcriptions/runpod-ack")
        reconnected = client.get("/api/stt/transcriptions/runpod-ack")
        acknowledged = client.post("/api/stt/transcriptions/runpod-ack/ack")
    app.dependency_overrides.clear()

    expected = {
        "segments": [{"start": 1.0, "end": 2.0, "text": " 테스트", "words": [
            {"start": 1.0, "end": 2.0, "word": " 테스트"},
        ]}],
        "engine": "whisperx-aligned-word-v1",
        "alignment": "ctc-forced-alignment-with-words",
    }
    assert completed.json()["result"] == expected
    assert reconnected.json()["result"] == expected
    assert acknowledged.status_code == 200
    assert "runpod-ack" not in jobs


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
    jobs, _ = _mock_transcription_store(monkeypatch)
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
    assert "runpod-cancel" not in jobs
    assert not audio_path.exists()


def test_cancel_before_runpod_start_prevents_remote_request(monkeypatch, tmp_path):
    app.dependency_overrides[get_current_user] = lambda: {"id": "user-001"}
    _, cancel_requests = _mock_transcription_store(monkeypatch)
    audio_path = Path(tmp_path) / "audio.mp3"
    audio_path.write_bytes(b"audio")
    monkeypatch.setattr("app.main._temporary_audio_path", lambda _: audio_path)
    started = []
    monkeypatch.setattr("app.main.start_transcription_with_whisper_api", lambda *args, **kwargs: started.append(True) or "runpod-never")
    with TestClient(app) as client:
        cancelled = client.post("/api/stt/transcriptions/client/local-before-start/cancel")
        response = client.post("/api/stt/transcriptions", json={"file_id": "a" * 32, "client_job_id": "local-before-start", "track_progress": True})
    app.dependency_overrides.clear()

    assert cancelled.status_code == 200
    assert response.status_code == 409
    assert started == []
    assert cancel_requests == set()
    assert not audio_path.exists()


def test_cancel_by_client_job_id_cancels_persisted_runpod_job(monkeypatch, tmp_path):
    app.dependency_overrides[get_current_user] = lambda: {"id": "user-001"}
    jobs, cancel_requests = _mock_transcription_store(monkeypatch)
    audio_path = Path(tmp_path) / "audio.mp3"
    audio_path.write_bytes(b"audio")
    jobs["runpod-client-cancel"] = {
        "user_id": "user-001",
        "runpod_job_id": "runpod-client-cancel",
        "client_job_id": "local-client-cancel",
        "file_id": "a" * 32,
        "status": "in_progress",
    }
    monkeypatch.setattr("app.main._temporary_audio_path", lambda _: audio_path)
    cancelled = []
    monkeypatch.setattr("app.main.cancel_transcription", lambda job_id: cancelled.append(job_id))
    with TestClient(app) as client:
        response = client.post("/api/stt/transcriptions/client/local-client-cancel/cancel")
    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert cancelled == ["runpod-client-cancel"]
    assert cancel_requests == set()
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
    jobs, _ = _mock_transcription_store(monkeypatch)
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
    jobs, _ = _mock_transcription_store(monkeypatch)
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

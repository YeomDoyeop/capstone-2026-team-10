from pathlib import Path

import requests

from app.services import server_media_service


class _Response:
    def __init__(self, payload=None, status_code=200):
        self.payload = payload or {}
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)
        return None

    def json(self):
        return self.payload

    def iter_lines(self, decode_unicode=False):
        yield 'data: {"status":"completed","progress":100,"message":"완료","result":{"segments":[]}}'

    def close(self):
        return None


def test_prepare_whisper_audio_converts_atomically_and_reuses_result(tmp_path, monkeypatch):
    source = tmp_path / "yt-data" / "video" / "video.mp3"
    destination = tmp_path / "yt-edit" / "video" / "video.whisper.mp3"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"source-audio")
    calls = []

    class _Completed:
        returncode = 0
        stderr = ""

    def run(args, **kwargs):
        calls.append((args, kwargs))
        assert args[args.index("-ac") + 1] == "1"
        assert args[args.index("-ar") + 1] == "16000"
        assert args[args.index("-b:a") + 1] == "64k"
        assert Path(args[-1]).parent == destination.parent
        Path(args[-1]).write_bytes(b"whisper-audio")
        return _Completed()

    monkeypatch.setattr(server_media_service, "get_ffmpeg", lambda: tmp_path / "ffmpeg")
    monkeypatch.setattr(server_media_service.subprocess, "run", run)

    assert server_media_service.prepare_whisper_audio(source, destination) == destination.resolve()
    assert destination.read_bytes() == b"whisper-audio"
    assert server_media_service.prepare_whisper_audio(source, destination) == destination.resolve()
    assert len(calls) == 1


def test_upload_audio_uses_and_preserves_prepared_mp3(tmp_path, monkeypatch):
    audio_path = tmp_path / "yt-edit" / "video" / "video.whisper.mp3"
    audio_path.parent.mkdir(parents=True)
    audio_path.write_bytes(b"audio")
    monkeypatch.setattr(server_media_service, "_server_url", lambda: "https://server.example")
    monkeypatch.setattr(
        server_media_service.requests,
        "post",
        lambda *_args, **_kwargs: _Response({"file_id": "a" * 32, "public_url": "https://server.example/files/audio.mp3"}),
    )
    server_media_service.upload_audio_for_transcription(audio_path, "Bearer token")

    assert audio_path.read_bytes() == b"audio"


def test_wait_for_transcription_sends_heartbeat_before_status(monkeypatch):
    posts = []
    monkeypatch.setattr(server_media_service, "_server_url", lambda: "https://server.example")
    monkeypatch.setattr(server_media_service, "get_whisper_heartbeat_seconds", lambda: 10)
    monkeypatch.setattr(
        server_media_service.requests,
        "post",
        lambda url, **kwargs: posts.append((url, kwargs)) or _Response(),
    )
    monkeypatch.setattr(
        server_media_service.requests,
        "get",
        lambda url, **kwargs: _Response({"status": "completed", "progress": 100, "message": "완료", "result": {"segments": []}}),
    )

    result = server_media_service._wait_for_transcription("runpod-001", "Bearer token", None)

    assert result == {"segments": []}
    assert posts[0][0].endswith("/api/stt/transcriptions/runpod-001/heartbeat")
    assert posts[0][1]["headers"]["Authorization"] == "Bearer token"


def test_wait_for_transcription_reads_current_token_for_each_remote_request(monkeypatch):
    used_tokens = []
    tokens = iter(["Bearer initial", "Bearer refreshed"])
    monkeypatch.setattr(server_media_service, "_server_url", lambda: "https://server.example")
    monkeypatch.setattr(server_media_service, "get_whisper_heartbeat_seconds", lambda: 10)
    monkeypatch.setattr(
        server_media_service.requests,
        "post",
        lambda _url, **kwargs: used_tokens.append(kwargs["headers"]["Authorization"]) or _Response(),
    )
    monkeypatch.setattr(
        server_media_service.requests,
        "get",
        lambda _url, **kwargs: used_tokens.append(kwargs["headers"]["Authorization"]) or _Response(),
    )

    server_media_service._wait_for_transcription("runpod-refresh", lambda: next(tokens), None)

    assert used_tokens == ["Bearer initial", "Bearer refreshed"]


def test_wait_for_transcription_reconnects_after_temporary_sse_disconnect(monkeypatch):
    attempts = []
    messages = []
    monkeypatch.setattr(server_media_service, "_server_url", lambda: "https://server.example")
    monkeypatch.setattr(server_media_service, "get_whisper_heartbeat_seconds", lambda: 10)
    monkeypatch.setattr(server_media_service.requests, "post", lambda *args, **kwargs: _Response())

    def get(*args, **kwargs):
        attempts.append((args, kwargs))
        if len(attempts) == 1:
            raise server_media_service.requests.ConnectionError("connection reset")
        return _Response()

    monkeypatch.setattr(server_media_service.requests, "get", get)
    monkeypatch.setattr(server_media_service.time, "sleep", lambda _: None)

    result = server_media_service._wait_for_transcription("runpod-003", "Bearer token", lambda progress, message: messages.append((progress, message)))

    assert result == {"segments": []}
    assert len(attempts) == 2
    assert messages[0][0] == 0
    assert "다시 연결" in messages[0][1]


def test_wait_for_transcription_reports_server_detail_instead_of_reconnecting(monkeypatch):
    calls = []
    monkeypatch.setattr(server_media_service, "_server_url", lambda: "https://server.example")
    monkeypatch.setattr(server_media_service, "get_whisper_heartbeat_seconds", lambda: 10)
    monkeypatch.setattr(server_media_service.requests, "post", lambda *_args, **_kwargs: _Response())
    monkeypatch.setattr(server_media_service.requests, "get", lambda *_args, **_kwargs: calls.append(1) or _Response({"detail": "Whisper API 응답 형식이 올바르지 않습니다."}, 502))

    try:
        server_media_service._wait_for_transcription("runpod-invalid", "Bearer token", None)
    except server_media_service.ServerMediaError as exc:
        assert str(exc) == "Whisper API 응답 형식이 올바르지 않습니다."
    else:
        raise AssertionError("서버 HTTP 오류를 일시적 SSE 단절로 처리하면 안 됩니다.")
    assert len(calls) == 1


def test_cancel_uploaded_transcription_calls_server_cancel(monkeypatch):
    calls = []
    monkeypatch.setattr(server_media_service, "_server_url", lambda: "https://server.example")
    monkeypatch.setattr(
        server_media_service.requests,
        "post",
        lambda url, **kwargs: calls.append((url, kwargs)) or _Response(),
    )

    server_media_service.cancel_uploaded_transcription("runpod-002", "Bearer token")

    assert calls[0][0].endswith("/api/stt/transcriptions/runpod-002/cancel")
    assert calls[0][1]["headers"]["Authorization"] == "Bearer token"


def test_acknowledge_transcription_result_calls_server_ack(monkeypatch):
    calls = []
    monkeypatch.setattr(server_media_service, "_server_url", lambda: "https://server.example")
    monkeypatch.setattr(
        server_media_service.requests,
        "post",
        lambda url, **kwargs: calls.append((url, kwargs)) or _Response(),
    )

    server_media_service.acknowledge_transcription_result("runpod-ack", "Bearer token")

    assert calls[0][0].endswith("/api/stt/transcriptions/runpod-ack/ack")
    assert calls[0][1]["headers"]["Authorization"] == "Bearer token"


def test_cancel_pending_uploaded_transcription_uses_client_job_id(monkeypatch):
    calls = []
    monkeypatch.setattr(server_media_service, "_server_url", lambda: "https://server.example")
    monkeypatch.setattr(
        server_media_service.requests,
        "post",
        lambda url, **kwargs: calls.append((url, kwargs)) or _Response(),
    )

    server_media_service.cancel_pending_uploaded_transcription("local-003", "Bearer token")

    assert calls[0][0].endswith("/api/stt/transcriptions/client/local-003/cancel")
    assert calls[0][1]["headers"]["Authorization"] == "Bearer token"

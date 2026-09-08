"""Whisper용 로컬 오디오 준비와 AVE 서버 전사 요청."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import requests

from app.config import get_ave_server_url, get_whisper_heartbeat_seconds
from app.services.toolchain import ToolchainError, ffmpeg as get_ffmpeg


logger = logging.getLogger(__name__)


class ServerMediaError(RuntimeError):
    pass


class TranscriptionCancelledError(ServerMediaError):
    pass


AccessTokenSource = str | Callable[[], str]


def _access_token(source: AccessTokenSource) -> str:
    value = source() if callable(source) else source
    if not isinstance(value, str) or not value.strip():
        raise ServerMediaError("원격 Whisper 전사를 위해 로그인 토큰이 필요합니다.")
    return value


def _server_error_detail(response: requests.Response, default: str) -> str:
    try:
        payload = response.json()
    except ValueError:
        return default
    if isinstance(payload, dict) and isinstance(payload.get("detail"), str) and payload["detail"].strip():
        return payload["detail"].strip()
    return default


def _raise_for_server_status(response: requests.Response, default: str) -> None:
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        detail = _server_error_detail(response, default)
        logger.error("AVE 서버가 Whisper 상태 요청을 거부했습니다: HTTP %s, %s", response.status_code, detail)
        raise ServerMediaError(detail) from exc


@dataclass(frozen=True)
class ServerMediaFile:
    file_id: str
    public_url: str


def prepare_whisper_audio(source: str | Path, destination: str | Path) -> Path:
    """Create and retain the deterministic 16 kHz mono Whisper input MP3."""

    source_path = Path(source).resolve()
    destination_path = Path(destination).resolve()
    if not source_path.is_file():
        raise ServerMediaError(f"최고 품질 원본 MP3를 찾을 수 없습니다: {source_path}")
    if destination_path.is_file() and destination_path.stat().st_size > 0:
        return destination_path
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    pending = destination_path.with_name(
        f".{destination_path.stem}.{uuid4().hex}.tmp.mp3"
    )
    try:
        ffmpeg = str(get_ffmpeg())
        completed = subprocess.run(
            [
                ffmpeg,
                "-y",
                "-i",
                str(source_path),
                "-map",
                "0:a:0",
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "libmp3lame",
                "-b:a",
                "64k",
                str(pending),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except (OSError, ToolchainError) as exc:
        raise ServerMediaError("Whisper 입력용 MP3를 생성하지 못했습니다.") from exc
    if completed.returncode != 0 or not pending.is_file() or pending.stat().st_size <= 0:
        raise ServerMediaError(completed.stderr[-1000:] or "Whisper 입력용 MP3 변환에 실패했습니다.")
    os.replace(pending, destination_path)
    return destination_path


def upload_audio_for_transcription(
    audio_path: str | Path, access_token: AccessTokenSource
) -> ServerMediaFile:
    prepared_audio_path = Path(audio_path).resolve()
    if not prepared_audio_path.is_file():
        raise ServerMediaError(f"전사용 MP3 파일을 찾을 수 없습니다: {prepared_audio_path}")
    endpoint = _server_url()
    try:
        with prepared_audio_path.open("rb") as audio:
            response = requests.post(
                f"{endpoint}/api/stt-files",
                headers={"Authorization": _access_token(access_token)},
                files={"file": (prepared_audio_path.name, audio, "audio/mpeg")},
                timeout=600,
            )
        response.raise_for_status()
        payload = response.json()
    except (OSError, requests.RequestException, ValueError) as exc:
        raise ServerMediaError("AVE 서버에 전사용 오디오를 업로드하지 못했습니다.") from exc
    file_id = payload.get("file_id")
    public_url = payload.get("public_url")
    if not isinstance(file_id, str) or not isinstance(public_url, str):
        raise ServerMediaError("AVE 서버의 임시 오디오 응답이 올바르지 않습니다.")
    return ServerMediaFile(file_id=file_id, public_url=public_url)


def transcribe_uploaded_audio(file_id: str, access_token: AccessTokenSource, *, client_job_id: str, server_job_id: str | None, language: str | None, initial_prompt: str | None, hotwords: str | None, speed: float, progress_callback: Callable[[int, str], None] | None = None, job_started_callback: Callable[[str], None] | None = None) -> dict:
    try:
        response = requests.post(
            f"{_server_url()}/api/stt/transcriptions",
            headers={"Authorization": _access_token(access_token), "Content-Type": "application/json"},
            json={"file_id": file_id, "client_job_id": client_job_id, "server_job_id": server_job_id, "language": language, "initial_prompt": initial_prompt, "hotwords": hotwords, "speed": speed, "track_progress": True},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise ServerMediaError("AVE 서버에 Whisper 전사를 요청하지 못했습니다.") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("job_id"), str):
        raise ServerMediaError("AVE 서버의 Whisper 전사 응답이 올바르지 않습니다.")
    if job_started_callback:
        job_started_callback(payload["job_id"])
    return _wait_for_transcription(payload["job_id"], access_token, progress_callback)


def _wait_for_transcription(job_id: str, access_token: AccessTokenSource, progress_callback: Callable[[int, str], None] | None) -> dict:
    deadline = time.monotonic() + 3700
    next_heartbeat = 0.0
    last_progress = 0
    last_connection_error: requests.RequestException | None = None
    while time.monotonic() < deadline:
        try:
            if time.monotonic() >= next_heartbeat:
                heartbeat = requests.post(f"{_server_url()}/api/stt/transcriptions/{job_id}/heartbeat", headers={"Authorization": _access_token(access_token)}, timeout=30)
                _raise_for_server_status(heartbeat, "AVE 서버의 Whisper heartbeat 요청이 실패했습니다.")
                next_heartbeat = time.monotonic() + get_whisper_heartbeat_seconds()
            response = requests.get(f"{_server_url()}/api/stt/transcriptions/{job_id}/events", headers={"Authorization": _access_token(access_token), "Accept": "text/event-stream"}, stream=True, timeout=(30, 35))
            try:
                _raise_for_server_status(response, "AVE 서버의 Whisper 상태 요청이 실패했습니다.")
                for line in response.iter_lines(decode_unicode=True):
                    if time.monotonic() >= next_heartbeat:
                        heartbeat = requests.post(f"{_server_url()}/api/stt/transcriptions/{job_id}/heartbeat", headers={"Authorization": _access_token(access_token)}, timeout=30)
                        _raise_for_server_status(heartbeat, "AVE 서버의 Whisper heartbeat 요청이 실패했습니다.")
                        next_heartbeat = time.monotonic() + get_whisper_heartbeat_seconds()
                    if not line or not line.startswith("data: "):
                        continue
                    payload = json.loads(line[6:])
                    if not isinstance(payload, dict):
                        raise ServerMediaError("AVE 서버의 Whisper 전사 상태 응답이 올바르지 않습니다.")
                    progress = payload.get("progress", 0)
                    message = payload.get("message", "Whisper 전사를 준비하는 중입니다.")
                    last_progress = progress if isinstance(progress, int) else last_progress
                    if progress_callback:
                        progress_callback(last_progress, message if isinstance(message, str) else "Whisper 전사를 준비하는 중입니다.")
                    if payload.get("status") == "completed":
                        result = payload.get("result")
                        if isinstance(result, dict) and isinstance(result.get("segments"), list):
                            return result
                        raise ServerMediaError("AVE 서버의 Whisper 전사 결과가 올바르지 않습니다.")
                    if payload.get("status") == "failed":
                        raise ServerMediaError(message if isinstance(message, str) else "Whisper 전사에 실패했습니다.")
                    if payload.get("status") in {"cancel_requested", "cancelled"}:
                        raise TranscriptionCancelledError(message if isinstance(message, str) else "Whisper 전사 작업이 취소되었습니다.")
            finally:
                response.close()
            # terminal 상태 없이 스트림이 종료된 경우에도 잠시 기다린 뒤 다시
            # 연결한다. 프록시가 유휴 SSE 연결을 닫는 경우를 포함한다.
            if progress_callback:
                progress_callback(last_progress, "AVE 서버 연결이 일시적으로 끊겼습니다. Whisper 전사 상태를 다시 연결하는 중입니다.")
            time.sleep(min(2, max(0, deadline - time.monotonic())))
        except requests.RequestException as exc:
            # 브라우저 탭 상태와 무관하게 네트워크·프록시가 SSE를 일시적으로
            # 종료할 수 있다. 작업과 heartbeat lease는 유지한 채 다시 연결한다.
            last_connection_error = exc
            logger.warning("Whisper 상태 SSE 연결이 끊겨 재연결합니다: %s: %s", type(exc).__name__, exc)
            if progress_callback:
                progress_callback(last_progress, "AVE 서버 연결이 일시적으로 끊겼습니다. Whisper 전사 상태를 다시 연결하는 중입니다.")
            time.sleep(min(2, max(0, deadline - time.monotonic())))
            continue
        except ValueError as exc:
            raise ServerMediaError("AVE 서버의 Whisper 전사 상태 응답을 해석하지 못했습니다.") from exc
    if last_connection_error is not None:
        raise ServerMediaError("AVE 서버의 Whisper 전사 SSE 연결을 다시 연결하지 못했습니다.") from last_connection_error
    raise ServerMediaError("Whisper 전사 작업 시간이 초과되었습니다.")


def cancel_uploaded_transcription(job_id: str, access_token: str) -> None:
    try:
        response = requests.post(f"{_server_url()}/api/stt/transcriptions/{job_id}/cancel", headers={"Authorization": access_token}, timeout=30)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise ServerMediaError("AVE 서버의 Whisper 전사 작업을 취소하지 못했습니다.") from exc


def acknowledge_transcription_result(job_id: str, access_token: str) -> None:
    try:
        response = requests.post(f"{_server_url()}/api/stt/transcriptions/{job_id}/ack", headers={"Authorization": access_token}, timeout=30)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise ServerMediaError("AVE 서버에 Whisper 전사 결과 확인을 전달하지 못했습니다.") from exc


def cancel_pending_uploaded_transcription(client_job_id: str, access_token: str) -> None:
    """RunPod 작업 ID가 아직 반환되지 않은 전사도 취소 대상으로 등록한다."""
    try:
        response = requests.post(
            f"{_server_url()}/api/stt/transcriptions/client/{client_job_id}/cancel",
            headers={"Authorization": access_token},
            timeout=30,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise ServerMediaError("AVE 서버에 Whisper 전사 취소 의도를 전달하지 못했습니다.") from exc


def _server_url() -> str:
    value = get_ave_server_url()
    if not value.startswith("https://"):
        raise ServerMediaError("AVE_SERVER_URL에 AVE 서버의 HTTPS 주소를 설정하세요.")
    return value

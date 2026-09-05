"""클라이언트의 분석 작업을 기록하는 AVE 서버 API."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.schemas import (
    AnalysisJobCreateRequest,
    AnalysisJobResponse,
    AnalysisResultRequest,
    AuthConfigResponse,
    AuthUserResponse,
    LLMGenerateRequest,
    LLMGenerateResponse,
    RemoteTranscriptionRequest,
    RemoteTranscriptionStatusResponse,
    TemporaryAudioResponse,
)
from app.config import get_public_base_url, get_stt_file_max_bytes, get_stt_files_dir, get_supabase_anon_key, get_supabase_url, get_transcription_lease_seconds, get_transcription_lease_sweep_seconds, get_transcription_result_ttl_seconds
from app.services.supabase_service import (
    create_transcription_job,
    clear_transcription_cancel_request,
    clear_expired_transcription_cancel_requests,
    create_job,
    get_expired_transcription_jobs,
    get_expired_transcription_results,
    get_auth_client,
    get_job,
    get_result,
    get_transcription_job,
    get_active_transcription_job_by_client_id,
    is_transcription_cancel_requested,
    request_transcription_cancel,
    delete_transcription_job,
    is_auth_configured,
    is_storage_configured,
    update_transcription_job,
    upsert_result,
)
from app.services.whisper_api_service import WhisperAPIError, cancel_transcription, get_transcription_status, start_transcription_with_whisper_api, transcribe_with_whisper_api
from app.services.llm_gateway import LLMGatewayError, generate_json


@asynccontextmanager
async def lifespan(_: FastAPI):
    task = asyncio.create_task(_lease_sweeper())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="AVE 서버 API", lifespan=lifespan)
auth_scheme = HTTPBearer(auto_error=False)
UPLOAD_CHUNK_SIZE = 1024 * 1024
logger = logging.getLogger(__name__)
TRANSCRIPTION_CANCEL_REQUEST_TTL_SECONDS = 600


def _normalized_transcript(output: dict) -> dict:
    segments = output.get("segments")
    if not isinstance(segments, list):
        raise ValueError("segments가 없습니다.")
    normalized = []
    for segment in segments:
        if not isinstance(segment, dict):
            raise ValueError("segment 형식이 올바르지 않습니다.")
        start, end, text = segment.get("start"), segment.get("end"), segment.get("text")
        if isinstance(start, bool) or isinstance(end, bool) or not isinstance(start, (int, float)) or not isinstance(end, (int, float)) or not isinstance(text, str) or end < start:
            raise ValueError("segment 값이 올바르지 않습니다.")
        normalized.append({"start": round(float(start), 3), "end": round(float(end), 3), "text": text})
    return {**output, "segments": normalized}


def _user_value(user: object, key: str):
    return user.get(key) if isinstance(user, dict) else getattr(user, key, None)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(auth_scheme),
):
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="인증 토큰이 필요합니다.")
    if not is_auth_configured():
        raise HTTPException(status_code=503, detail="Supabase 인증 설정이 없습니다.")
    try:
        response = get_auth_client().auth.get_user(credentials.credentials)
        user = _user_value(response, "user")
        if not _user_value(user, "id"):
            raise ValueError("사용자 식별자가 없습니다.")
        return user
    except Exception as exc:
        raise HTTPException(status_code=401, detail="유효하지 않거나 만료된 토큰입니다.") from exc


def _require_storage() -> None:
    if not is_storage_configured():
        raise HTTPException(status_code=503, detail="Supabase 서버 저장소 설정이 없습니다.")


def _job_response(job: dict) -> dict:
    return {key: job[key] for key in ("id", "client_job_id", "status", "progress")}


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/api/auth/config", response_model=AuthConfigResponse)
async def auth_config():
    if not is_auth_configured():
        raise HTTPException(status_code=503, detail="Supabase 인증 설정이 없습니다.")
    return {"supabase_url": get_supabase_url(), "supabase_anon_key": get_supabase_anon_key()}


@app.get("/api/auth/me", response_model=AuthUserResponse)
async def auth_me(user=Depends(get_current_user)):
    return {"id": str(_user_value(user, "id")), "email": _user_value(user, "email")}


@app.post("/api/llm/generate", response_model=LLMGenerateResponse)
async def generate_llm_response(request: LLMGenerateRequest, user=Depends(get_current_user)):
    del user
    try:
        text = await asyncio.to_thread(generate_json, request.provider, request.system, request.prompt, model=request.model, response_schema=request.response_schema)
    except LLMGatewayError as exc:
        raise HTTPException(status_code=503 if exc.unavailable else 502, detail=str(exc)) from exc
    return {"text": text}


def _temporary_audio_path(file_id: str) -> Path:
    if len(file_id) != 32 or any(char not in "0123456789abcdef" for char in file_id):
        raise HTTPException(status_code=404, detail="임시 오디오 파일을 찾을 수 없습니다.")
    return get_stt_files_dir() / f"{file_id}.mp3"


def _runpod_progress(status: dict) -> tuple[int, str]:
    """RunPod 상태 응답에서 worker가 마지막으로 보고한 진행도를 읽는다.

    RunPod SDK 및 endpoint 버전에 따라 progress_update payload가 ``progress``
    필드가 아니라 작업 중인 ``output`` 필드에 노출될 수 있다.
    """
    default_message = "Whisper 전사를 준비하는 중입니다."
    for update in (status.get("progress"), status.get("output")):
        if isinstance(update, dict):
            value = update.get("progress")
            message = update.get("message")
        else:
            value = update
            message = None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return max(0, min(100, round(value))), message if isinstance(message, str) else default_message
    return 0, default_message


def _lease_values() -> dict[str, str]:
    now = datetime.now(timezone.utc)
    return {
        "last_heartbeat_at": now.isoformat(),
        "lease_expires_at": (now + timedelta(seconds=get_transcription_lease_seconds())).isoformat(),
    }


def _cancel_request_expires_at() -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=TRANSCRIPTION_CANCEL_REQUEST_TTL_SECONDS)).isoformat()


async def _delete_transient_transcription(job: dict) -> None:
    await asyncio.to_thread(delete_transcription_job, str(job["user_id"]), str(job["runpod_job_id"]))


def _lease_has_expired(job: dict) -> bool:
    value = job.get("lease_expires_at")
    if not isinstance(value, str):
        return False
    try:
        expires_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        logger.warning("Whisper 작업 lease 형식이 올바르지 않습니다: %s", job.get("runpod_job_id"))
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at <= datetime.now(timezone.utc)


async def _cancel_persisted_transcription(job: dict, reason: str) -> dict:
    user_id = str(job["user_id"])
    runpod_job_id = str(job["runpod_job_id"])
    if job.get("status") in {"completed", "failed", "cancelled"}:
        return job
    requested = datetime.now(timezone.utc).isoformat()
    job = await asyncio.to_thread(update_transcription_job, user_id, runpod_job_id, {"status": "cancel_requested", "message": reason, "cancel_requested_at": requested}) or job
    try:
        await asyncio.to_thread(cancel_transcription, runpod_job_id)
    except WhisperAPIError:
        # 취소 요청과 RunPod 종료가 경합하면 실제 terminal 상태를 확정한다.
        try:
            current = await asyncio.to_thread(get_transcription_status, runpod_job_id)
            state = str(current.get("status", "")).upper()
        except WhisperAPIError:
            state = ""
        if state in {"COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT"}:
            status = "cancelled" if state == "CANCELLED" else ("completed" if state == "COMPLETED" else "failed")
            _temporary_audio_path(str(job["file_id"])).unlink(missing_ok=True)
            return await asyncio.to_thread(
                update_transcription_job,
                user_id,
                runpod_job_id,
                {
                    "status": status,
                    "progress": 100,
                    "message": "Whisper 전사가 이미 완료되었습니다." if status == "completed" else reason,
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                },
            ) or job
        logger.warning("RunPod 전사 작업 취소를 다시 시도합니다: %s", runpod_job_id)
        return job
    _temporary_audio_path(str(job["file_id"])).unlink(missing_ok=True)
    completed = datetime.now(timezone.utc).isoformat()
    return await asyncio.to_thread(update_transcription_job, user_id, runpod_job_id, {"status": "cancelled", "progress": 100, "message": reason, "completed_at": completed}) or job


async def _lease_sweeper() -> None:
    while True:
        await asyncio.sleep(get_transcription_lease_sweep_seconds())
        await _sweep_expired_transcriptions()


async def _sweep_expired_transcriptions() -> None:
    if not is_storage_configured():
        return
    try:
        expired = await asyncio.to_thread(get_expired_transcription_jobs, datetime.now(timezone.utc).isoformat())
        for job in expired:
            job = await _cancel_persisted_transcription(job, "클라이언트 heartbeat가 만료되어 Whisper 작업을 취소했습니다.")
            if job.get("status") in {"cancelled", "failed"}:
                await _delete_transient_transcription(job)
        result_expired = await asyncio.to_thread(get_expired_transcription_results, datetime.now(timezone.utc).isoformat())
        for job in result_expired:
            await _delete_transient_transcription(job)
        await asyncio.to_thread(clear_expired_transcription_cancel_requests, datetime.now(timezone.utc).isoformat())
    except Exception:
        logger.exception("만료된 Whisper 작업 정리에 실패했습니다.")


@app.post("/api/stt-files", response_model=TemporaryAudioResponse, status_code=201)
async def upload_temporary_audio(file: UploadFile = File(...), user=Depends(get_current_user)):
    del user
    if Path(file.filename or "").suffix.lower() != ".mp3":
        raise HTTPException(status_code=415, detail="전사용 MP3 파일만 업로드할 수 있습니다.")
    public_base_url = get_public_base_url()
    if not public_base_url.startswith("https://"):
        raise HTTPException(status_code=503, detail="AVE_PUBLIC_BASE_URL 설정이 없습니다.")
    destination_dir = get_stt_files_dir()
    destination_dir.mkdir(parents=True, exist_ok=True)
    file_id = uuid4().hex
    destination = _temporary_audio_path(file_id)
    written = 0
    try:
        with destination.open("xb") as output:
            while chunk := await file.read(UPLOAD_CHUNK_SIZE):
                written += len(chunk)
                if written > get_stt_file_max_bytes():
                    raise HTTPException(status_code=413, detail="전사용 오디오 파일이 허용 크기를 초과했습니다.")
                output.write(chunk)
    except HTTPException:
        destination.unlink(missing_ok=True)
        raise
    except OSError as exc:
        destination.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail="임시 오디오 파일을 저장하지 못했습니다.") from exc
    finally:
        await file.close()
    return {"file_id": file_id, "public_url": f"{public_base_url}/files/{destination.name}"}


@app.post("/api/stt/transcriptions")
async def transcribe_temporary_audio(request: RemoteTranscriptionRequest, user=Depends(get_current_user)):
    audio_path = _temporary_audio_path(request.file_id)
    if not audio_path.is_file():
        raise HTTPException(status_code=404, detail="임시 오디오 파일을 찾을 수 없습니다.")
    if not request.track_progress:
        del user
        try:
            return await asyncio.to_thread(
                transcribe_with_whisper_api,
                f"{get_public_base_url()}/files/{audio_path.name}",
                language=request.language,
                initial_prompt=request.initial_prompt,
                hotwords=request.hotwords,
                speed=request.speed,
            )
        except WhisperAPIError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        finally:
            audio_path.unlink(missing_ok=True)
    if not request.client_job_id:
        raise HTTPException(status_code=422, detail="진행도 추적 전사에는 client_job_id가 필요합니다.")
    _require_storage()
    user_id = str(_user_value(user, "id"))
    if await asyncio.to_thread(is_transcription_cancel_requested, user_id, request.client_job_id):
        audio_path.unlink(missing_ok=True)
        await asyncio.to_thread(clear_transcription_cancel_request, user_id, request.client_job_id)
        raise HTTPException(status_code=409, detail="취소된 Whisper 전사 요청입니다.")
    try:
        runpod_job_id = await asyncio.to_thread(
            start_transcription_with_whisper_api,
            f"{get_public_base_url()}/files/{audio_path.name}",
            language=request.language,
            initial_prompt=request.initial_prompt,
            hotwords=request.hotwords,
            speed=request.speed,
        )
    except WhisperAPIError as exc:
        audio_path.unlink(missing_ok=True)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    try:
        values = {
            "runpod_job_id": runpod_job_id,
            "client_job_id": request.client_job_id,
            "server_job_id": request.server_job_id,
            "file_id": request.file_id,
            "phase": "transcription",
            "status": "queued",
            "progress": 0,
            "message": "Whisper 전사 작업을 대기열에 등록했습니다.",
            **_lease_values(),
        }
        await asyncio.to_thread(create_transcription_job, user_id, values)
    except Exception as exc:
        try:
            await asyncio.to_thread(cancel_transcription, runpod_job_id)
        except WhisperAPIError:
            logger.warning("저장 실패 후 RunPod 전사 작업을 취소하지 못했습니다: %s", runpod_job_id)
        audio_path.unlink(missing_ok=True)
        raise HTTPException(status_code=503, detail="Whisper 작업 추적을 저장하지 못했습니다.") from exc
    if await asyncio.to_thread(is_transcription_cancel_requested, user_id, request.client_job_id):
        job = await _cancel_persisted_transcription(values | {"user_id": user_id}, "클라이언트가 Whisper 전사 준비 중 취소했습니다.")
        await asyncio.to_thread(clear_transcription_cancel_request, user_id, request.client_job_id)
        if job.get("status") in {"cancelled", "completed", "failed"}:
            await _delete_transient_transcription(job)
        raise HTTPException(status_code=409, detail="취소된 Whisper 전사 요청입니다.")
    return JSONResponse(status_code=202, content={"job_id": runpod_job_id, "status": "queued", "progress": 0, "message": "Whisper 전사 작업을 대기열에 등록했습니다.", "lease_expires_at": values["lease_expires_at"]})


@app.get("/api/stt/transcriptions/{job_id}", response_model=RemoteTranscriptionStatusResponse)
async def read_transcription_status(job_id: str, user=Depends(get_current_user)):
    _require_storage()
    user_id = str(_user_value(user, "id"))
    job = await asyncio.to_thread(get_transcription_job, user_id, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="전사 작업을 찾을 수 없습니다.")
    if job.get("status") in {"cancelled", "completed", "failed"}:
        response = {"job_id": job_id, "status": job["status"], "progress": job.get("progress", 100), "message": job.get("message") or "Whisper 전사 작업이 종료되었습니다.", "lease_expires_at": job.get("lease_expires_at")}
        if job.get("status") == "completed" and isinstance(job.get("result"), dict):
            response["result"] = _normalized_transcript(job["result"])
            return response
        await _delete_transient_transcription(job)
        return response
    if _lease_has_expired(job):
        job = await _cancel_persisted_transcription(job, "클라이언트 heartbeat가 만료되어 Whisper 작업을 취소했습니다.")
        response = {"job_id": job_id, "status": job["status"], "progress": job.get("progress", 0), "message": job.get("message") or "Whisper 전사 취소를 요청했습니다.", "lease_expires_at": job.get("lease_expires_at")}
        await _delete_transient_transcription(job)
        return response
    try:
        runpod_status = await asyncio.to_thread(get_transcription_status, job_id)
    except WhisperAPIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    state = str(runpod_status.get("status", "")).upper()
    progress, message = _runpod_progress(runpod_status)
    response: dict = {
        "job_id": job_id,
        "status": "cancel_requested" if job.get("status") == "cancel_requested" else (state.lower() or "queued"),
        "progress": progress,
        "message": message,
        "lease_expires_at": job.get("lease_expires_at"),
    }
    values: dict = {
        "status": "cancel_requested" if job.get("status") == "cancel_requested" else ("in_progress" if state == "IN_PROGRESS" else "queued"),
        "progress": progress,
        "message": message,
    }
    if state == "COMPLETED":
        output = runpod_status.get("output")
        if not isinstance(output, dict) or not isinstance(output.get("segments"), list):
            _temporary_audio_path(str(job["file_id"])).unlink(missing_ok=True)
            await asyncio.to_thread(update_transcription_job, user_id, job_id, {"status": "failed", "progress": 100, "message": "Whisper API 응답 형식이 올바르지 않습니다.", "completed_at": datetime.now(timezone.utc).isoformat()})
            await _delete_transient_transcription(job)
            raise HTTPException(status_code=502, detail="Whisper API 응답 형식이 올바르지 않습니다.")
        if isinstance(output.get("error"), dict):
            response.update(status="failed", progress=100, message=str(output["error"].get("message") or "Whisper 전사에 실패했습니다."))
        else:
            try:
                normalized_output = _normalized_transcript(output)
            except ValueError as exc:
                _temporary_audio_path(str(job["file_id"])).unlink(missing_ok=True)
                await asyncio.to_thread(update_transcription_job, user_id, job_id, {"status": "failed", "progress": 100, "message": "Whisper API 전사 결과 형식이 올바르지 않습니다.", "completed_at": datetime.now(timezone.utc).isoformat()})
                await _delete_transient_transcription(job)
                raise HTTPException(status_code=502, detail="Whisper API 전사 결과 형식이 올바르지 않습니다.") from exc
            response.update(status="completed", progress=100, message="Whisper 전사가 완료되었습니다.", result=normalized_output)
    elif state in {"FAILED", "CANCELLED", "TIMED_OUT"}:
        response.update(status="cancelled" if state == "CANCELLED" else "failed", progress=100, message=str(runpod_status.get("error") or "Whisper 전사에 실패했습니다."))
    if state in {"COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT"}:
        _temporary_audio_path(str(job["file_id"])).unlink(missing_ok=True)
        values.update(status=response["status"], progress=100, message=response["message"], completed_at=datetime.now(timezone.utc).isoformat())
        if response["status"] == "completed":
            values.update(result=response["result"], result_expires_at=(datetime.now(timezone.utc) + timedelta(seconds=get_transcription_result_ttl_seconds())).isoformat())
    await asyncio.to_thread(update_transcription_job, user_id, job_id, values)
    if state in {"FAILED", "CANCELLED", "TIMED_OUT"}:
        await _delete_transient_transcription(job)
    return response


@app.post("/api/stt/transcriptions/{job_id}/ack")
async def acknowledge_transcription_result(job_id: str, user=Depends(get_current_user)):
    _require_storage()
    job = await asyncio.to_thread(get_transcription_job, str(_user_value(user, "id")), job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="전사 작업을 찾을 수 없습니다.")
    if job.get("status") != "completed" or not isinstance(job.get("result"), dict):
        raise HTTPException(status_code=409, detail="확인할 완료 전사 결과가 없습니다.")
    await _delete_transient_transcription(job)
    return {"job_id": job_id, "status": "acknowledged"}


@app.get("/api/stt/transcriptions/{job_id}/events")
async def stream_transcription_status(job_id: str, user=Depends(get_current_user)):
    """RunPod REST 상태를 SSE로 전달한다. 클라이언트 명령은 REST로 유지한다."""
    async def events():
        while True:
            status = await read_transcription_status(job_id, user)
            yield f"event: status\ndata: {json.dumps(status, ensure_ascii=False)}\n\n"
            if status.get("status") in {"completed", "failed", "cancelled"}:
                return
            await asyncio.sleep(1)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/stt/transcriptions/{job_id}/heartbeat", response_model=RemoteTranscriptionStatusResponse)
async def heartbeat_transcription(job_id: str, user=Depends(get_current_user)):
    _require_storage()
    user_id = str(_user_value(user, "id"))
    job = await asyncio.to_thread(get_transcription_job, user_id, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="전사 작업을 찾을 수 없습니다.")
    if job.get("status") in {"completed", "failed", "cancelled"}:
        response = {"job_id": job_id, "status": job["status"], "progress": job.get("progress", 100), "message": job.get("message") or "Whisper 전사 작업이 종료되었습니다.", "lease_expires_at": job.get("lease_expires_at")}
        if job.get("status") == "completed" and isinstance(job.get("result"), dict):
            return response
        await _delete_transient_transcription(job)
        return response
    if _lease_has_expired(job):
        job = await _cancel_persisted_transcription(job, "클라이언트 heartbeat가 만료되어 Whisper 작업을 취소했습니다.")
        response = {"job_id": job_id, "status": job["status"], "progress": job.get("progress", 0), "message": job.get("message") or "Whisper 전사 취소를 요청했습니다.", "lease_expires_at": job.get("lease_expires_at")}
        await _delete_transient_transcription(job)
        return response
    values = _lease_values()
    job = await asyncio.to_thread(update_transcription_job, user_id, job_id, values) or job
    return {"job_id": job_id, "status": job["status"], "progress": job.get("progress", 0), "message": job.get("message") or "Whisper 전사를 준비하는 중입니다.", "lease_expires_at": job.get("lease_expires_at")}


@app.post("/api/stt/transcriptions/{job_id}/cancel", response_model=RemoteTranscriptionStatusResponse)
async def cancel_transcription_job(job_id: str, user=Depends(get_current_user)):
    _require_storage()
    job = await asyncio.to_thread(get_transcription_job, str(_user_value(user, "id")), job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="전사 작업을 찾을 수 없습니다.")
    job = await _cancel_persisted_transcription(job, "클라이언트 요청으로 Whisper 작업을 취소했습니다.")
    response = {"job_id": job_id, "status": job["status"], "progress": job.get("progress", 0), "message": job.get("message") or "Whisper 전사 취소를 요청했습니다.", "lease_expires_at": job.get("lease_expires_at")}
    if job.get("status") in {"cancelled", "completed", "failed"}:
        await _delete_transient_transcription(job)
    return response


@app.post("/api/stt/transcriptions/client/{client_job_id}/cancel")
async def cancel_transcription_by_client_job_id(client_job_id: str, user=Depends(get_current_user)):
    """RunPod ID가 아직 없는 전사 시작 경합을 포함해 취소 의도를 보관한다."""
    _require_storage()
    user_id = str(_user_value(user, "id"))
    await asyncio.to_thread(request_transcription_cancel, user_id, client_job_id, _cancel_request_expires_at())
    job = await asyncio.to_thread(get_active_transcription_job_by_client_id, user_id, client_job_id)
    if job is not None:
        job = await _cancel_persisted_transcription(job, "클라이언트 요청으로 Whisper 작업을 취소했습니다.")
        await asyncio.to_thread(clear_transcription_cancel_request, user_id, client_job_id)
        if job.get("status") in {"cancelled", "completed", "failed"}:
            await _delete_transient_transcription(job)
        return {"job_id": job["runpod_job_id"], "status": job["status"], "message": job.get("message") or "Whisper 전사 작업을 취소했습니다."}
    return {"client_job_id": client_job_id, "status": "cancel_requested", "message": "Whisper 전사 준비 단계의 취소를 등록했습니다."}


@app.post("/api/analysis-jobs", response_model=AnalysisJobResponse, status_code=201)
async def create_analysis_job(request: AnalysisJobCreateRequest, user=Depends(get_current_user)):
    _require_storage()
    try:
        job = create_job(
            str(_user_value(user, "id")),
            {
                "id": str(uuid4()),
                **request.model_dump(),
                "status": "completed",
                "progress": 100,
                "completed_at": datetime.now(timezone.utc).isoformat(),
            },
        )
    except Exception as exc:
        logger.exception("분석 작업 이력 생성에 실패했습니다.")
        raise HTTPException(
            status_code=503,
            detail="분석 이력 DB가 준비되지 않았습니다. ave-server/docs/supabase_schema.sql을 Supabase SQL Editor에서 실행하세요.",
        ) from exc
    return _job_response(job)


@app.get("/api/analysis-jobs/{job_id}", response_model=AnalysisJobResponse)
async def read_analysis_job(job_id: str, user=Depends(get_current_user)):
    _require_storage()
    job = get_job(str(_user_value(user, "id")), job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="작업을 찾을 수 없습니다.")
    return _job_response(job)


@app.put("/api/analysis-jobs/{job_id}/result")
async def save_analysis_result(job_id: str, request: AnalysisResultRequest, user=Depends(get_current_user)):
    _require_storage()
    for segment in request.segments:
        if segment.end_ms <= segment.start_ms:
            raise HTTPException(status_code=422, detail="구간 종료 시각은 시작 시각보다 커야 합니다.")
    result = upsert_result(str(_user_value(user, "id")), job_id, request.model_dump())
    if result is None:
        raise HTTPException(status_code=404, detail="작업을 찾을 수 없습니다.")
    return result


@app.get("/api/analysis-jobs/{job_id}/result")
async def read_analysis_result(job_id: str, user=Depends(get_current_user)):
    _require_storage()
    result = get_result(str(_user_value(user, "id")), job_id)
    if result is None:
        raise HTTPException(status_code=404, detail="분석 결과를 찾을 수 없습니다.")
    return result

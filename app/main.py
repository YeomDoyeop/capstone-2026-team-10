"""클라이언트의 분석 작업을 기록하는 AVE 서버 API."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.schemas import (
    AnalysisJobCreateRequest,
    AnalysisJobResponse,
    AnalysisJobStatusRequest,
    AnalysisResultRequest,
    AuthConfigResponse,
    AuthUserResponse,
    LLMGenerateRequest,
    LLMGenerateResponse,
    RemoteTranscriptionRequest,
    TemporaryAudioResponse,
)
from app.config import get_public_base_url, get_stt_file_max_bytes, get_stt_files_dir, get_supabase_anon_key, get_supabase_url
from app.services.supabase_service import (
    create_job,
    get_auth_client,
    get_job,
    get_result,
    is_auth_configured,
    is_storage_configured,
    update_job,
    upsert_result,
)
from app.services.whisper_api_service import WhisperAPIError, transcribe_with_whisper_api
from app.services.llm_gateway import LLMGatewayError, generate_json


app = FastAPI(title="AVE 서버 API")
auth_scheme = HTTPBearer(auto_error=False)
UPLOAD_CHUNK_SIZE = 1024 * 1024
logger = logging.getLogger(__name__)


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
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"text": text}


def _temporary_audio_path(file_id: str) -> Path:
    if len(file_id) != 32 or any(char not in "0123456789abcdef" for char in file_id):
        raise HTTPException(status_code=404, detail="임시 오디오 파일을 찾을 수 없습니다.")
    return get_stt_files_dir() / f"{file_id}.mp3"


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
    del user
    audio_path = _temporary_audio_path(request.file_id)
    if not audio_path.is_file():
        raise HTTPException(status_code=404, detail="임시 오디오 파일을 찾을 수 없습니다.")
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


@app.post("/api/analysis-jobs", response_model=AnalysisJobResponse, status_code=201)
async def create_analysis_job(request: AnalysisJobCreateRequest, user=Depends(get_current_user)):
    _require_storage()
    try:
        job = create_job(
            str(_user_value(user, "id")),
            {
                "id": str(uuid4()),
                **request.model_dump(),
                "status": "queued",
                "progress": 0,
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


@app.patch("/api/analysis-jobs/{job_id}", response_model=AnalysisJobResponse)
async def update_analysis_job(job_id: str, request: AnalysisJobStatusRequest, user=Depends(get_current_user)):
    _require_storage()
    values = request.model_dump()
    if request.status in {"completed", "failed"}:
        values["completed_at"] = datetime.now(timezone.utc).isoformat()
    job = update_job(str(_user_value(user, "id")), job_id, values)
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

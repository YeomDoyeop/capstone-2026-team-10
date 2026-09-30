from __future__ import annotations

import json
import asyncio
import re
import requests
import time
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.config import get_ave_server_url, get_database_root, get_media_root
from app.schemas import (
    AuthUserResponse,
    PublicConfigResponse,
    YouTubeMetadataRequest,
    YouTubeMetadataMaterialsRequest,
    WhisperSettingsRequest,
    WhisperPreparationRequest,
    LiveEditRequest,
    ScriptPreviewRequest,
    SegmentSelectionRequest,
)
from app.services.live_youtube_service import (
    LiveYouTubeError,
    extract_video_id,
    get_video_metadata,
    download_metadata_materials,
)
from app.services.live_edit_pipeline import (
    LiveEditCancelled,
    LiveEditPaused,
    LiveEditPipeline,
    LiveEditPipelineError,
    has_default_whisper_transcript_cache,
)
from app.services.llm_analysis_service import LLMAnalysisError, LLMAnalysisService
from app.services.prompt_store import (
    PromptStoreError,
    delete_user_prompt,
    list_user_prompts,
    save_user_prompt,
)
from app.services.youtube_importer import YouTubeImporter
from app.services.local_job_store import LocalJobStore
from app.services.server_job_service import (
    ServerJobError,
    create_job as create_server_job,
    save_result as save_server_result,
)
from app.services.server_media_service import (
    ServerMediaError,
    TranscriptionCancelledError,
    cancel_pending_uploaded_transcription,
    cancel_uploaded_transcription,
)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
REACT_UI_DIR = STATIC_DIR / "ui"


app = FastAPI(title="Automatic Video Editor MVP")
auth_scheme = HTTPBearer(auto_error=False)
LIVE_EDIT_JOBS: dict[str, dict] = {}
LIVE_EDIT_CANCEL_REQUESTS: set[str] = set()
METADATA_MATERIAL_JOBS: dict[str, dict] = {}
WHISPER_TRANSCRIPT_JOBS: dict[str, dict] = {}
WHISPER_TRANSCRIPT_CANCEL_REQUESTS: set[str] = set()
LIVE_EDIT_ACCESS_TOKENS: dict[str, str] = {}
EDIT_JOB_LOCKS: dict[str, asyncio.Lock] = {}
METADATA_JOB_RETENTION = timedelta(minutes=10)
# The tray runs in this same local process and needs to cancel jobs during
# shutdown even after a browser session has expired. This capability never
# leaves loopback IPC and is not exposed to the browser UI.
LOCAL_CONTROL_TOKEN = secrets.token_urlsafe(32)

if (REACT_UI_DIR / "assets").exists():
    app.mount(
        "/ui/assets",
        StaticFiles(directory=REACT_UI_DIR / "assets"),
        name="react-ui-assets",
    )


def _user_value(user, key: str):
    if isinstance(user, dict):
        return user.get(key)
    return getattr(user, key, None)


def _user_id(user) -> str:
    value = _user_value(user, "id")
    if not value:
        raise HTTPException(status_code=401, detail="Invalid login session.")
    return str(value)


def _job_for_user(job_id: str, user) -> dict:
    job = LIVE_EDIT_JOBS.get(job_id) or _completed_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="AI 편집 작업을 찾을 수 없습니다.")
    # Completed records written before ownership was introduced are not a
    # supported compatibility target in the development workspace.
    if job.get("owner_id") != _user_id(user):
        raise HTTPException(status_code=404, detail="AI 편집 작업을 찾을 수 없습니다.")
    return job


def _cleanup_edit_job_state(job_id: str) -> None:
    LIVE_EDIT_JOBS.pop(job_id, None)
    _cleanup_edit_transient_state(job_id)


def _cleanup_edit_transient_state(job_id: str) -> None:
    LIVE_EDIT_ACCESS_TOKENS.pop(job_id, None)
    LIVE_EDIT_CANCEL_REQUESTS.discard(job_id)
    EDIT_JOB_LOCKS.pop(job_id, None)


def _cleanup_expired_metadata_jobs() -> None:
    now = datetime.now(timezone.utc)
    for job_id, job in list(METADATA_MATERIAL_JOBS.items()):
        completed_at = job.get("completed_at")
        if not isinstance(completed_at, datetime):
            continue
        if now - completed_at >= METADATA_JOB_RETENTION:
            METADATA_MATERIAL_JOBS.pop(job_id, None)
    for job_id, job in list(WHISPER_TRANSCRIPT_JOBS.items()):
        completed_at = job.get("completed_at")
        if (
            isinstance(completed_at, datetime)
            and now - completed_at >= METADATA_JOB_RETENTION
        ):
            WHISPER_TRANSCRIPT_JOBS.pop(job_id, None)
            WHISPER_TRANSCRIPT_CANCEL_REQUESTS.discard(job_id)


async def _expire_metadata_material_job(job_id: str) -> None:
    """Bound terminal polling state even when the browser never reconnects."""

    await asyncio.sleep(METADATA_JOB_RETENTION.total_seconds())
    job = METADATA_MATERIAL_JOBS.get(job_id)
    if job and job.get("status") in {"completed", "failed"}:
        METADATA_MATERIAL_JOBS.pop(job_id, None)


async def _expire_whisper_transcript_job(
    job_id: str, expected_job: dict | None = None
) -> None:
    await asyncio.sleep(METADATA_JOB_RETENTION.total_seconds())
    job = WHISPER_TRANSCRIPT_JOBS.get(job_id)
    if (
        job
        and (expected_job is None or job is expected_job)
        and job.get("status") in {"completed", "failed", "cancelled"}
    ):
        WHISPER_TRANSCRIPT_JOBS.pop(job_id, None)
        WHISPER_TRANSCRIPT_CANCEL_REQUESTS.discard(job_id)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(auth_scheme),
):
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="Google login is required.")
    try:
        response = requests.get(
            f"{_server_url()}/api/auth/me",
            headers={"Authorization": f"Bearer {credentials.credentials}"},
            timeout=15,
        )
        response.raise_for_status()
        user = response.json()
        user_id = user.get("id") if isinstance(user, dict) else None
    except (requests.RequestException, ValueError) as exc:
        raise HTTPException(
            status_code=401, detail="Invalid or expired login session."
        ) from exc

    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid login session.")
    return user


def _server_url() -> str:
    value = get_ave_server_url()
    if not value.startswith("https://"):
        raise HTTPException(status_code=503, detail="AVE_SERVER_URL is not configured.")
    return value


def _new_edit_job_id(vod_url: str) -> str:
    """Use a source-stable, filesystem-safe job ID without retaining counters."""

    return f"{extract_video_id(vod_url)}.{int(time.time() * 1000):x}"


@app.get("/")
async def index():
    index_path = REACT_UI_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(
            status_code=404, detail="React UI를 빌드하세요: ui에서 npm run build"
        )
    return FileResponse(index_path)


@app.get("/ui")
@app.get("/ui/")
async def react_index():
    index_path = REACT_UI_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(
            status_code=404, detail="React UI를 빌드하세요: ui에서 npm run build"
        )
    return FileResponse(index_path)


@app.get("/ui/favicon.svg")
async def react_favicon():
    favicon_path = REACT_UI_DIR / "favicon.svg"
    if not favicon_path.exists():
        raise HTTPException(
            status_code=404, detail="React UI 파비콘을 찾을 수 없습니다."
        )
    return FileResponse(
        favicon_path,
        media_type="image/svg+xml",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/config", response_model=PublicConfigResponse)
async def public_config():
    try:
        response = requests.get(f"{_server_url()}/api/auth/config", timeout=15)
        response.raise_for_status()
        value = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise HTTPException(
            status_code=503, detail="AVE 서버 인증 설정을 불러오지 못했습니다."
        ) from exc
    if not isinstance(value, dict):
        raise HTTPException(
            status_code=503, detail="AVE 서버 인증 설정 응답이 올바르지 않습니다."
        )
    return value


@app.get("/api/auth/me", response_model=AuthUserResponse)
async def auth_me(user=Depends(get_current_user)):
    return {"id": str(_user_value(user, "id")), "email": _user_value(user, "email")}


@app.get("/api/prompts/user")
async def get_user_prompts(user=Depends(get_current_user)):
    del user
    try:
        return {"prompts": list_user_prompts()}
    except PromptStoreError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.put("/api/prompts/user/{prompt_id}")
async def put_user_prompt(
    prompt_id: str, value: dict, create: bool = False, user=Depends(get_current_user)
):
    del user
    try:
        return save_user_prompt(prompt_id, value, create=create)
    except PromptStoreError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.delete("/api/prompts/user/{prompt_id}")
async def remove_user_prompt(prompt_id: str, user=Depends(get_current_user)):
    del user
    try:
        delete_user_prompt(prompt_id)
        return {"status": "deleted"}
    except PromptStoreError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _update_live_edit_job(job_id: str, **values) -> None:
    job = LIVE_EDIT_JOBS.get(job_id)
    if job is not None:
        job.update(values)


def _analysis_task_name(message: str) -> str:
    if "음량" in message or "오디오" in message:
        return "음량 분석"
    if "채팅" in message:
        return "채팅 분석"
    if "댓글" in message:
        return "댓글 분석"
    if "히트맵" in message:
        return "히트맵 분석"
    if "하이라이트" in message or "구간을 선택" in message:
        return "하이라이트 선정"
    if "LLM" in message or "챕터" in message or "섹션" in message:
        return "LLM 영상 분석"
    return "영상 분석 준비"


def _raise_if_cancel_requested(job_id: str) -> None:
    if (
        job_id in LIVE_EDIT_CANCEL_REQUESTS
        or LIVE_EDIT_JOBS.get(job_id, {}).get("status") == "cancel_requested"
    ):
        raise LiveEditCancelled("사용자가 작업을 취소했습니다.")


def _completed_job(job_id: str) -> dict | None:
    return LocalJobStore(get_database_root()).get_completed(job_id)


async def _discard_failed_edit_job(job_id: str, error: Exception) -> None:
    """Notify the browser, then discard only transient in-memory state."""

    detail = str(error).strip() or "알 수 없는 오류"
    job = LIVE_EDIT_JOBS.get(job_id)
    if job is not None:
        _update_live_edit_job(
            job_id,
            status="failed",
            message=f"AI 편집 작업이 실패했습니다: {detail}",
            error=detail,
        )
        # 실패 이력은 저장하지 않지만, SSE가 마지막 오류를 브라우저에 보낼 짧은
        # 시간은 필요하다. 작업 폴더는 진단을 위해 보존한다.
        await asyncio.sleep(1.2)
    _cleanup_edit_job_state(job_id)


async def _pause_edit_job(job_id: str, error: Exception) -> None:
    detail = str(error).strip() or "LLM 요청이 중단되었습니다."
    _update_live_edit_job(
        job_id,
        status="paused",
        phase="analysis",
        message=f"{detail} 재개하면 완료된 LLM 응답을 재사용합니다.",
        error=detail,
    )
    _cleanup_edit_transient_state(job_id)


async def _run_live_edit_job(
    job_id: str,
    request: LiveEditRequest,
    server_access_token: str | None = None,
) -> None:
    server_job_id: str | None = None

    def report_analysis(progress: int, message: str) -> None:
        _raise_if_cancel_requested(job_id)
        _update_live_edit_job(
            job_id,
            progress=progress,
            phase="analysis",
            task_name=_analysis_task_name(message),
            message=message,
        )

    def report_transcription(progress: int, message: str) -> None:
        _raise_if_cancel_requested(job_id)
        _update_live_edit_job(
            job_id,
            progress=progress,
            transcription_progress=progress,
            phase="transcription",
            message=message,
        )

    def report_whisper_preparing() -> None:
        _raise_if_cancel_requested(job_id)
        _update_live_edit_job(
            job_id,
            whisper_preparing=True,
            phase="transcription",
            message="Whisper 전사를 준비하는 중입니다.",
        )

    try:
        _update_live_edit_job(
            job_id,
            status="running",
            progress=3,
            phase="analysis",
            task_name="영상 분석 준비",
            message="2단계에서 준비한 영상 자료를 확인하는 중입니다.",
        )
        vod_id = extract_video_id(request.vod_url)

        pipeline = LiveEditPipeline(get_media_root())
        result = await asyncio.to_thread(
            pipeline.run,
            job_id=job_id,
            vod_url=request.vod_url,
            genre=request.genre,
            criteria_prompt=request.criteria_prompt,
            llm_provider=request.llm_provider,
            target_seconds=request.target_duration_seconds,
            chapter_split_mode=request.chapter_split_mode,
            use_timestamp_comments=request.use_timestamp_comments,
            use_chat_score=request.use_chat_score,
            transcription_source=request.transcription_source,
            transcript_language=request.transcript_language,
            stt_language=request.stt_language,
            stt_initial_prompt=request.stt_initial_prompt,
            stt_hotwords=request.stt_hotwords,
            stt_speed=request.stt_speed,
            defer_render=True,
            stop_after_structure=bool(
                LIVE_EDIT_JOBS.get(job_id, {}).get("stop_after_structure")
            ),
            server_access_token=server_access_token,
            server_job_id=server_job_id,
            progress_callback=report_analysis,
            cancel_callback=lambda: _raise_if_cancel_requested(job_id),
            whisper_progress_callback=report_transcription,
            whisper_preparing_callback=report_whisper_preparing,
            whisper_job_started_callback=lambda runpod_job_id: _update_live_edit_job(
                job_id,
                runpod_job_id=runpod_job_id,
                phase="transcription",
                message="Whisper 전사 작업을 시작했습니다.",
            ),
        )
        result["vod_video_id"] = vod_id
        if result.get("awaiting_scoring"):
            _update_live_edit_job(
                job_id,
                status="awaiting_scoring",
                progress=100,
                phase="analysis",
                task_name="스크립트 분할 및 점수 계산",
                message="분할과 점수 계산을 완료했습니다. 다음을 눌러 챕터 내 필수 관계를 판별하세요.",
                result=result,
            )
            _cleanup_edit_transient_state(job_id)
            LIVE_EDIT_CANCEL_REQUESTS.discard(job_id)
        elif result.get("awaiting_selection"):
            _update_live_edit_job(
                job_id,
                status="awaiting_selection",
                progress=100,
                phase="selection",
                message="AI 분석이 완료되었습니다. 원하는 구간을 선택하세요.",
                result=result,
            )
            _cleanup_edit_transient_state(job_id)
            LIVE_EDIT_CANCEL_REQUESTS.discard(job_id)
        else:
            # 렌더링 성공은 로컬 완료의 기준이다. 완료 직후 재접속하거나
            # 동기화를 재시도해도 동일 client_job_id로 서버 이력을 재사용한다.
            LocalJobStore(get_database_root()).save_completed(
                job_id, result, owner_id=LIVE_EDIT_JOBS.get(job_id, {}).get("owner_id")
            )
            sync_warning = None
            if server_access_token:
                try:
                    server_job_id = await asyncio.to_thread(
                        create_server_job,
                        server_access_token,
                        client_job_id=job_id,
                        source_id=vod_id,
                        source_url=request.vod_url,
                    )
                    await asyncio.to_thread(
                        save_server_result, server_access_token, server_job_id, result
                    )
                except ServerJobError as exc:
                    sync_warning = str(exc)
            _update_live_edit_job(
                job_id,
                status="completed",
                progress=100,
                phase="render",
                message=(
                    "AI 영상 편집이 완료되었습니다."
                    if not sync_warning
                    else f"영상 생성은 완료됐지만 {sync_warning}"
                ),
                result=result,
            )
            _cleanup_edit_transient_state(job_id)
    except LiveEditCancelled as exc:
        _cleanup_edit_job_state(job_id)
    except LiveEditPaused as exc:
        await _pause_edit_job(job_id, exc)
    except (LiveYouTubeError, LiveEditPipelineError, ServerJobError) as exc:
        await _discard_failed_edit_job(job_id, exc)
    except Exception as exc:
        await _discard_failed_edit_job(job_id, exc)


@app.post("/api/youtube/edit/start", status_code=202)
async def start_live_edit(
    request: LiveEditRequest,
    authorization: str | None = Header(default=None),
    user=Depends(get_current_user),
):
    if not authorization:
        raise HTTPException(
            status_code=401, detail="AVE 서버 연동에는 로그인 토큰이 필요합니다."
        )
    try:
        job_id = request.job_id
    except LiveYouTubeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if Path(job_id).name != job_id:
        raise HTTPException(status_code=400, detail="잘못된 편집 작업 ID입니다.")
    try:
        metadata = await asyncio.to_thread(
            get_video_metadata, request.vod_url, refresh=False
        )
    except LiveYouTubeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if not metadata.get("subtitles_available") and not metadata.get(
        "captions_available"
    ):
        request = request.model_copy(
            update={"transcription_source": "whisper_api", "transcript_language": None}
        )
    existing = LIVE_EDIT_JOBS.get(job_id)
    if existing:
        if existing.get("owner_id") != _user_id(user):
            raise HTTPException(
                status_code=409, detail="다른 작업에 사용 중인 편집 작업 ID입니다."
            )
        if existing.get("status") not in {
            "awaiting_scoring",
            "awaiting_selection",
            "completed",
            "failed",
            "cancelled",
        }:
            raise HTTPException(
                status_code=409,
                detail="이미 실행 중이거나 재개 대기 중인 편집 작업 ID입니다.",
            )
    if request.transcription_source == "whisper_api":
        prepared = WHISPER_TRANSCRIPT_JOBS.get(job_id)
        if (
            not prepared
            or prepared.get("owner_id") != _user_id(user)
            or prepared.get("status") != "completed"
            or prepared.get("source_url") != request.vod_url
        ):
            raise HTTPException(
                status_code=409, detail="완료된 Whisper 준비 작업을 확인할 수 없습니다."
            )
    if existing:
        # 사용자가 결과 확인 후 이전 단계로 돌아가 STT·설정을 다시 준비한
        # 경우에는 같은 작업 ID로 분석 결과를 새로 만들 수 있어야 한다.
        _cleanup_edit_job_state(job_id)
    LIVE_EDIT_JOBS[job_id] = {
        "job_id": job_id,
        "status": "queued",
        "progress": 0,
        "phase": "analysis",
        "task_name": "영상 분석 준비",
        "message": "AI 편집 작업을 준비하는 중입니다.",
        "transcription_source": request.transcription_source,
        "owner_id": _user_id(user),
        "request": request.model_dump(),
        "stop_after_structure": True,
    }
    LIVE_EDIT_ACCESS_TOKENS[job_id] = authorization
    asyncio.create_task(_run_live_edit_job(job_id, request, authorization))
    return LIVE_EDIT_JOBS[job_id]


@app.post("/api/youtube/edit/script")
async def preview_edit_script(
    request: ScriptPreviewRequest,
    user=Depends(get_current_user),
):
    del user
    try:
        segments = await asyncio.to_thread(
            LiveEditPipeline(get_media_root()).load_script_segments,
            job_id=request.job_id,
            vod_url=request.vod_url,
            transcription_source=request.transcription_source,
            transcript_language=request.transcript_language,
        )
        return {"segments": segments}
    except LiveEditPipelineError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/youtube/edit/{job_id}/score", status_code=202)
async def score_live_edit(
    job_id: str,
    authorization: str | None = Header(default=None),
    user=Depends(get_current_user),
    target_duration_seconds: int | None = None,
):
    if not authorization:
        raise HTTPException(
            status_code=401, detail="LLM 호출에는 로그인 토큰이 필요합니다."
        )
    job = _job_for_user(job_id, user)
    if job.get("status") != "awaiting_scoring":
        raise HTTPException(status_code=409, detail="링크를 판별할 점수 결과가 없습니다.")
    try:
        request = LiveEditRequest.model_validate(job.get("request"))
    except Exception as exc:
        raise HTTPException(
            status_code=409, detail="분석 작업 설정을 찾을 수 없습니다."
        ) from exc
    if target_duration_seconds is not None:
        if not 60 <= target_duration_seconds <= 7200:
            raise HTTPException(
                status_code=422,
                detail="목표 길이는 60초에서 7200초 사이여야 합니다.",
            )
        request = LiveEditRequest.model_validate({
            **request.model_dump(), "target_duration_seconds": target_duration_seconds
        })
        job["request"] = request.model_dump()
    job.update(
        status="queued",
        progress=90,
        phase="analysis",
        task_name="챕터 내 필수 관계 판별",
        message="점수가 높은 섹션부터 챕터 내 필수 관계를 판별하는 중입니다.",
        stop_after_structure=False,
        error=None,
    )
    LIVE_EDIT_ACCESS_TOKENS[job_id] = authorization or ""
    asyncio.create_task(_run_live_edit_job(job_id, request, authorization))
    return job


@app.post("/api/youtube/edit/{job_id}/resume", status_code=202)
async def resume_live_edit(
    job_id: str,
    authorization: str | None = Header(default=None),
    user=Depends(get_current_user),
):
    job = _job_for_user(job_id, user)
    if job.get("status") != "paused":
        raise HTTPException(
            status_code=409, detail="재개할 수 있는 일시중지 작업이 아닙니다."
        )
    try:
        request = LiveEditRequest.model_validate(job.get("request"))
    except Exception as exc:
        raise HTTPException(
            status_code=409, detail="재개할 작업 설정을 찾을 수 없습니다."
        ) from exc
    LIVE_EDIT_ACCESS_TOKENS[job_id] = authorization or ""
    _update_live_edit_job(
        job_id,
        status="queued",
        task_name="LLM 영상 분석",
        error=None,
        message="중단된 LLM 분석을 재개하는 중입니다.",
    )
    asyncio.create_task(_run_live_edit_job(job_id, request, authorization))
    return LIVE_EDIT_JOBS[job_id]


@app.post("/api/youtube/metadata")
async def youtube_metadata(request: YouTubeMetadataRequest):
    """Return the phase-one preview data using yt-dlp only."""
    try:
        video_id = extract_video_id(request.url)
        # 같은 영상의 재작업은 yt-data 원본 정보를 그대로 다시 보여 준다.
        metadata = await asyncio.to_thread(
            get_video_metadata, request.url, refresh=False
        )
        metadata["default_whisper_cache_available"] = (
            has_default_whisper_transcript_cache(get_media_root(), video_id)
        )
        return metadata
    except LiveYouTubeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/youtube/metadata/whisper-settings")
async def recommend_whisper_settings(
    request: WhisperSettingsRequest,
    authorization: str | None = Header(default=None),
    user=Depends(get_current_user),
):
    if not authorization:
        raise HTTPException(
            status_code=401, detail="LLM 호출에는 로그인 토큰이 필요합니다."
        )
    del user
    try:
        service = LLMAnalysisService(
            provider=request.llm_provider, server_access_token=authorization
        )
        return await asyncio.to_thread(
            service.recommend_whisper_settings,
            request.model_dump(exclude={"llm_provider"}),
        )
    except LLMAnalysisError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/youtube/metadata/whisper-transcript")
async def prepare_whisper_transcript(
    request: WhisperPreparationRequest,
    authorization: str | None = Header(default=None),
    user=Depends(get_current_user),
):
    if not authorization:
        raise HTTPException(
            status_code=401, detail="Whisper 전사에는 로그인 토큰이 필요합니다."
        )
    try:
        job_id = request.job_id
        if Path(job_id).name != job_id:
            raise LiveYouTubeError("잘못된 편집 작업 ID입니다.")
        result = await asyncio.to_thread(
            LiveEditPipeline(get_media_root()).prepare_whisper_transcript,
            job_id=job_id,
            vod_url=request.url,
            llm_provider=request.llm_provider,
            stt_language=request.stt_language,
            stt_initial_prompt=request.stt_initial_prompt,
            stt_hotwords=request.stt_hotwords,
            stt_speed=request.stt_speed,
            server_access_token=authorization,
        )
        WHISPER_TRANSCRIPT_JOBS[job_id] = {
            "job_id": job_id,
            "owner_id": _user_id(user),
            "source_url": request.url,
            "status": "completed",
            "progress": 100,
            "message": "Whisper 전사를 완료했습니다.",
            "result": result,
            "completed_at": datetime.now(timezone.utc),
        }
        return result
    except (LiveYouTubeError, LiveEditPipelineError, ServerMediaError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


async def _run_whisper_transcript_job(
    job_id: str, request: WhisperPreparationRequest
) -> None:
    job = WHISPER_TRANSCRIPT_JOBS[job_id]

    def cancelled() -> None:
        if job_id in WHISPER_TRANSCRIPT_CANCEL_REQUESTS:
            raise LiveEditCancelled("Whisper 전사 작업을 취소했습니다.")

    def progress(value: int, message: str) -> None:
        cancelled()
        job.update({"progress": max(0, min(100, value)), "message": message})

    def started(remote_job_id: str) -> None:
        job["remote_job_id"] = remote_job_id

    try:
        result = await asyncio.to_thread(
            LiveEditPipeline(get_media_root()).prepare_whisper_transcript,
            job_id=job_id,
            vod_url=request.url,
            llm_provider=request.llm_provider,
            stt_language=request.stt_language,
            stt_initial_prompt=request.stt_initial_prompt,
            stt_hotwords=request.stt_hotwords,
            stt_speed=request.stt_speed,
            server_access_token=lambda: str(job.get("access_token") or ""),
            progress_callback=progress,
            cancel_callback=cancelled,
            whisper_job_started_callback=started,
        )
        job.update(
            {
                "status": "completed",
                "progress": 100,
                "message": "Whisper 전사를 완료했습니다.",
                "result": result,
                "completed_at": datetime.now(timezone.utc),
            }
        )
    except (LiveEditCancelled, TranscriptionCancelledError):
        job.update(
            {
                "status": "cancelled",
                "message": "Whisper 전사 작업을 취소했습니다.",
                "completed_at": datetime.now(timezone.utc),
            }
        )
    except LiveEditPaused as exc:
        detail = str(exc)
        job.update(
            {
                "status": "paused",
                "message": f"{detail} 재시도하면 완료된 LLM 응답을 재사용합니다.",
                "error": detail,
            }
        )
    except (LiveYouTubeError, LiveEditPipelineError, ServerMediaError) as exc:
        job.update(
            {
                "status": "failed",
                "message": str(exc),
                "error": str(exc),
                "completed_at": datetime.now(timezone.utc),
            }
        )
    except Exception as exc:
        job.update(
            {
                "status": "failed",
                "message": "Whisper 전사에 실패했습니다.",
                "error": str(exc),
                "completed_at": datetime.now(timezone.utc),
            }
        )
    finally:
        asyncio.create_task(_expire_whisper_transcript_job(job_id, job))


@app.post("/api/youtube/metadata/whisper-transcript/start", status_code=202)
async def start_whisper_transcript(
    request: WhisperPreparationRequest,
    authorization: str | None = Header(default=None),
    user=Depends(get_current_user),
):
    if not authorization:
        raise HTTPException(
            status_code=401, detail="Whisper 전사에는 로그인 토큰이 필요합니다."
        )
    _cleanup_expired_metadata_jobs()
    try:
        job_id = request.job_id
    except LiveYouTubeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if Path(job_id).name != job_id:
        raise HTTPException(status_code=400, detail="잘못된 편집 작업 ID입니다.")
    existing = WHISPER_TRANSCRIPT_JOBS.get(job_id)
    if existing and (
        existing.get("owner_id") != _user_id(user)
        or existing.get("source_url") != request.url
    ):
        raise HTTPException(
            status_code=409, detail="다른 작업에 사용 중인 편집 작업 ID입니다."
        )
    if existing and existing.get("status") in {"running", "cancel_requested"}:
        raise HTTPException(
            status_code=409, detail="이미 실행 중인 Whisper 전사 작업입니다."
        )
    WHISPER_TRANSCRIPT_CANCEL_REQUESTS.discard(job_id)
    WHISPER_TRANSCRIPT_JOBS[job_id] = {
        "job_id": job_id,
        "owner_id": _user_id(user),
        "source_url": request.url,
        "status": "running",
        "progress": 0,
        "message": "Whisper 전사를 준비하는 중입니다.",
        "access_token": authorization,
        "client_job_id": job_id,
    }
    asyncio.create_task(_run_whisper_transcript_job(job_id, request))
    return {
        key: value
        for key, value in WHISPER_TRANSCRIPT_JOBS[job_id].items()
        if key != "access_token"
    }


@app.get("/api/youtube/metadata/whisper-transcript/{job_id}")
async def get_whisper_transcript_job(
    job_id: str,
    authorization: str | None = Header(default=None),
    user=Depends(get_current_user),
):
    job = WHISPER_TRANSCRIPT_JOBS.get(job_id)
    if not job or job.get("owner_id") != _user_id(user):
        raise HTTPException(
            status_code=404, detail="Whisper 전사 작업을 찾을 수 없습니다."
        )
    if authorization:
        job["access_token"] = authorization
    return {key: value for key, value in job.items() if key != "access_token"}


@app.post("/api/youtube/metadata/whisper-transcript/{job_id}/cancel")
async def cancel_whisper_transcript_job(
    job_id: str,
    authorization: str | None = Header(default=None),
    user=Depends(get_current_user),
):
    job = WHISPER_TRANSCRIPT_JOBS.get(job_id)
    if not job or job.get("owner_id") != _user_id(user):
        raise HTTPException(
            status_code=404, detail="Whisper 전사 작업을 찾을 수 없습니다."
        )
    if authorization:
        job["access_token"] = authorization
    if job.get("status") in {"completed", "failed", "cancelled"}:
        return {key: value for key, value in job.items() if key != "access_token"}
    WHISPER_TRANSCRIPT_CANCEL_REQUESTS.add(job_id)
    job.update(
        {"status": "cancel_requested", "message": "Whisper 전사 취소를 요청했습니다."}
    )
    try:
        remote_job_id = job.get("remote_job_id")
        if isinstance(remote_job_id, str) and remote_job_id:
            await asyncio.to_thread(
                cancel_uploaded_transcription, remote_job_id, job["access_token"]
            )
        else:
            await asyncio.to_thread(
                cancel_pending_uploaded_transcription,
                job["client_job_id"],
                job["access_token"],
            )
    except ServerMediaError as exc:
        job["message"] = f"취소를 요청했습니다. 서버 확인을 다시 시도합니다: {exc}"
    return {key: value for key, value in job.items() if key != "access_token"}


@app.post("/api/youtube/metadata/materials")
async def youtube_metadata_materials(request: YouTubeMetadataMaterialsRequest):
    try:
        extract_video_id(request.url)
        selections = {
            key: getattr(request, key)
            for key in (
                "comments",
                "chat",
                "subtitles",
                "captions",
                "subtitle_language",
                "caption_language",
            )
        }
        return await asyncio.to_thread(
            download_metadata_materials, request.url, selections
        )
    except LiveYouTubeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


async def _run_metadata_material_job(
    job_id: str, request: YouTubeMetadataMaterialsRequest
) -> None:
    job = METADATA_MATERIAL_JOBS[job_id]

    def update(progress: int, message: str) -> None:
        job.update({"progress": max(0, min(100, progress)), "message": message})

    try:
        selections = {
            key: getattr(request, key)
            for key in (
                "comments",
                "chat",
                "subtitles",
                "captions",
                "subtitle_language",
                "caption_language",
            )
        }
        result = await asyncio.to_thread(
            download_metadata_materials, request.url, selections, update
        )
        job.update(
            {
                "status": "completed",
                "progress": 100,
                "message": "추가 메타데이터 준비를 완료했습니다.",
                "result": result,
                "completed_at": datetime.now(timezone.utc),
            }
        )
    except LiveYouTubeError as exc:
        job.update(
            {
                "status": "failed",
                "message": str(exc),
                "error": str(exc),
                "completed_at": datetime.now(timezone.utc),
            }
        )
    except Exception as exc:
        job.update(
            {
                "status": "failed",
                "message": "추가 메타데이터 다운로드에 실패했습니다.",
                "error": str(exc),
                "completed_at": datetime.now(timezone.utc),
            }
        )
    finally:
        if job.get("status") in {"completed", "failed"}:
            asyncio.create_task(_expire_metadata_material_job(job_id))


@app.post("/api/youtube/metadata/materials/start", status_code=202)
async def start_youtube_metadata_materials(
    request: YouTubeMetadataMaterialsRequest, user=Depends(get_current_user)
):
    _cleanup_expired_metadata_jobs()
    try:
        extract_video_id(request.url)
    except LiveYouTubeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    job_id = request.job_id or _new_edit_job_id(request.url)
    if Path(job_id).name != job_id:
        raise HTTPException(status_code=400, detail="잘못된 편집 작업 ID입니다.")
    existing = METADATA_MATERIAL_JOBS.get(job_id)
    if existing and (
        existing.get("owner_id") != _user_id(user)
        or existing.get("source_url") != request.url
    ):
        raise HTTPException(
            status_code=409, detail="다른 작업에 사용 중인 편집 작업 ID입니다."
        )
    if existing and existing.get("status") == "running":
        raise HTTPException(
            status_code=409, detail="이미 실행 중인 추가 메타데이터 작업입니다."
        )
    METADATA_MATERIAL_JOBS[job_id] = {
        "job_id": job_id,
        "owner_id": _user_id(user),
        "source_url": request.url,
        "status": "running",
        "progress": 0,
        "message": "추가 메타데이터 다운로드를 준비하는 중입니다.",
    }
    asyncio.create_task(_run_metadata_material_job(job_id, request))
    return METADATA_MATERIAL_JOBS[job_id]


@app.get("/api/youtube/metadata/materials/{job_id}")
async def get_youtube_metadata_material_job(
    job_id: str, user=Depends(get_current_user)
):
    _cleanup_expired_metadata_jobs()
    job = METADATA_MATERIAL_JOBS.get(job_id)
    if not job:
        raise HTTPException(
            status_code=404, detail="추가 메타데이터 작업을 찾을 수 없습니다."
        )
    if job.get("owner_id") != _user_id(user):
        raise HTTPException(
            status_code=404, detail="추가 메타데이터 작업을 찾을 수 없습니다."
        )
    response = dict(job)
    # 결과 파일은 보존하지만, UI가 끝 상태를 읽은 뒤 진행 상태는 남기지 않는다.
    if response.get("status") in {"completed", "failed"}:
        METADATA_MATERIAL_JOBS.pop(job_id, None)
    return response


@app.get("/api/youtube/thumbnail/{video_id}/{filename}")
async def youtube_thumbnail(video_id: str, filename: str):
    if (
        not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id)
        or Path(filename).name != filename
    ):
        raise HTTPException(status_code=404, detail="썸네일을 찾을 수 없습니다.")
    path = get_media_root() / "yt-data" / video_id / "thumbnails" / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="썸네일을 찾을 수 없습니다.")
    return FileResponse(path, headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/youtube/metadata/{video_id}/media/source")
async def youtube_metadata_source_video(video_id: str, user=Depends(get_current_user)):
    """2단계에서 준비된 원본 영상을 분석 시작 전 미리보기에 제공한다."""

    del user
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise HTTPException(status_code=400, detail="잘못된 YouTube 영상 ID입니다.")
    cached = YouTubeImporter(get_media_root()).find_complete_cached_import(
        f"https://www.youtube.com/watch?v={video_id}", video_id
    )
    if not cached:
        raise HTTPException(status_code=404, detail="준비된 원본 영상을 찾을 수 없습니다.")
    path = Path(str(cached["video_path"]))
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    if not path.is_file():
        raise HTTPException(status_code=404, detail="준비된 원본 영상을 찾을 수 없습니다.")
    return FileResponse(
        path, media_type="video/mp4", headers={"Cache-Control": "no-store"}
    )


@app.get("/api/youtube/edit/status/{job_id}")
async def live_edit_status(job_id: str, user=Depends(get_current_user)):
    return _job_for_user(job_id, user)


@app.get("/api/youtube/edit/{job_id}/events")
async def live_edit_events(job_id: str, user=Depends(get_current_user)):
    """로컬 작업 상태를 브라우저에 SSE로 전달한다."""
    _job_for_user(job_id, user)

    async def events():
        previous = ""
        yield "retry: 1000\n\n"
        while True:
            job = LIVE_EDIT_JOBS.get(job_id) or _completed_job(job_id)
            if job is None:
                yield 'event: error\ndata: {"detail":"AI 편집 작업을 찾을 수 없습니다."}\n\n'
                return
            payload = json.dumps(job, ensure_ascii=False)
            if payload != previous:
                yield f"data: {payload}\n\n"
                previous = payload
            # 선택 대기 상태는 4단계 진입에 필요한 마지막 이벤트다. 스트림을
            # 즉시 닫으면 프록시 버퍼가 이 이벤트를 버릴 수 있으므로 heartbeat를
            # 유지한다. 완료 뒤에는 클라이언트가 연결을 닫는다.
            if job.get("status") in {
                "completed",
                "failed",
                "cancelled",
                "paused",
                "awaiting_scoring",
            }:
                return
            yield ": keep-alive\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/youtube/edit/active")
async def active_live_edit_jobs(user=Depends(get_current_user)):
    terminal = {"completed", "failed", "cancelled"}
    owner_id = _user_id(user)
    return {
        "jobs": [
            job
            for job in LIVE_EDIT_JOBS.values()
            if job.get("status") not in terminal and job.get("owner_id") == owner_id
        ]
    }


@app.post("/api/youtube/edit/{job_id}/cancel")
async def cancel_live_edit(
    job_id: str,
    authorization: str | None = Header(default=None),
    user=Depends(get_current_user),
):
    if not job_id or Path(job_id).name != job_id:
        raise HTTPException(status_code=400, detail="잘못된 편집 작업 ID입니다.")
    # 취소는 이미 종료된 작업에도 멱등적으로 성공해야 한다. 작업 폴더는
    # 사용자의 로컬 진단 자료이므로 취소 여부와 관계없이 보존한다.
    owned_job = _job_for_user(job_id, user)
    is_live = job_id in LIVE_EDIT_JOBS
    job = LIVE_EDIT_JOBS.get(job_id)
    if job is None:
        if owned_job.get("status") == "completed":
            return owned_job
        _cleanup_edit_job_state(job_id)
        return {
            "job_id": job_id,
            "status": "cancelled",
            "message": "작업은 이미 종료되었습니다. 로컬 작업 파일은 보존됩니다.",
        }
    if job.get("status") in {"completed", "awaiting_selection"}:
        return job
    LIVE_EDIT_CANCEL_REQUESTS.add(job_id)
    runpod_job_id = job.get("runpod_job_id")
    if isinstance(runpod_job_id, str) and runpod_job_id and not authorization:
        raise HTTPException(
            status_code=401, detail="Whisper 작업 취소에는 로그인 토큰이 필요합니다."
        )
    job = {
        **job,
        "status": "cancel_requested" if is_live else "cancelled",
        "progress": job.get("progress", 0) if is_live else 100,
        "phase": "cancelled",
        "message": (
            "작업 취소를 요청했습니다."
            if is_live
            else "실행 중이 아닌 이전 작업을 종료했습니다."
        ),
        "error": None,
    }
    if is_live:
        LIVE_EDIT_JOBS[job_id] = job
    access_token = authorization or LIVE_EDIT_ACCESS_TOKENS.get(job_id)
    if (
        job.get("transcription_source") == "whisper_api"
        and job.get("whisper_preparing")
        and not runpod_job_id
        and access_token
    ):
        try:
            await asyncio.to_thread(
                cancel_pending_uploaded_transcription, job_id, access_token
            )
        except ServerMediaError as exc:
            job["message"] = f"취소를 요청했습니다. 서버 확인을 다시 시도합니다: {exc}"
            if is_live:
                LIVE_EDIT_JOBS[job_id] = job
    if isinstance(runpod_job_id, str) and runpod_job_id:
        try:
            await asyncio.to_thread(
                cancel_uploaded_transcription, runpod_job_id, access_token or ""
            )
        except ServerMediaError as exc:
            # 서버가 이미 RunPod 취소를 시작했거나 일시적으로 응답하지 않을 수
            # 있으므로 로컬 상태는 유지한다. heartbeat 중단 뒤 server lease가 재시도한다.
            job["message"] = f"취소를 요청했습니다. 서버 확인을 다시 시도합니다: {exc}"
            if is_live:
                LIVE_EDIT_JOBS[job_id] = job
    if is_live:
        # 실행 중인 작업은 worker가 취소 신호를 확인하고 정리하게 둔다.
        # 여기서 transient state를 지우면 FFmpeg가 취소를 관찰할 수 없다.
        return job
    _cleanup_edit_job_state(job_id)
    return {
        "job_id": job_id,
        "status": "cancelled",
        "message": "작업을 취소했습니다. 로컬 작업 파일은 보존됩니다.",
    }


@app.post("/api/youtube/edit/cancel-active")
async def cancel_all_live_edit_jobs(user=Depends(get_current_user)):
    """트레이 종료 시 이 프로세스가 보유한 작업을 모두 중단한다."""
    cancelled = 0
    terminal = {"completed", "failed", "cancelled"}
    owner_id = _user_id(user)
    jobs = dict(LIVE_EDIT_JOBS)
    for job_id, job in jobs.items():
        if job.get("status") in terminal or job.get("owner_id") != owner_id:
            continue
        await cancel_live_edit(job_id, LIVE_EDIT_ACCESS_TOKENS.get(job_id), user)
        cancelled += 1
    return {"cancelled": cancelled}


@app.post("/api/internal/cancel-active")
async def cancel_all_live_edit_jobs_from_tray(
    x_ave_local_control: str | None = Header(default=None),
):
    if not x_ave_local_control or not secrets.compare_digest(
        x_ave_local_control, LOCAL_CONTROL_TOKEN
    ):
        raise HTTPException(
            status_code=401, detail="로컬 트레이 제어 권한이 필요합니다."
        )
    cancelled = 0
    for job_id, job in list(LIVE_EDIT_JOBS.items()):
        if job.get("status") in {"completed", "failed", "cancelled", "paused"}:
            continue
        await cancel_live_edit(
            job_id, LIVE_EDIT_ACCESS_TOKENS.get(job_id), {"id": job.get("owner_id")}
        )
        cancelled += 1
    return {"cancelled": cancelled}


async def _run_segment_selection_job(
    job_id: str,
    request: SegmentSelectionRequest,
    server_access_token: str | None = None,
) -> None:
    lock = EDIT_JOB_LOCKS.setdefault(job_id, asyncio.Lock())
    try:
        async with lock:
            previous_result = dict(LIVE_EDIT_JOBS.get(job_id, {}).get("result") or {})
            # 렌더링 성공 여부와 관계없이 사용자가 확정한 선택은 현재 편집
            # 상태다. 취소 시 기존 AI 추천 선택으로 돌아가지 않도록 렌더링 전에
            # 결과 사본에 반영한다.
            previous_plan = dict(previous_result.get("analysis_plan") or {})
            requested_ids = [str(value) for value in request.segment_ids]
            candidates_by_id = {
                str(item.get("segment_id")): item
                for item in previous_plan.get("candidates") or []
                if isinstance(item, dict)
            }
            previous_plan.update(
                {
                    "selected_segment_ids": requested_ids,
                    "clips": [
                        {
                            key: candidate[key]
                            for key in ("segment_id", "start", "end", "llm_score")
                            if key in candidate
                        }
                        for segment_id in requested_ids
                        if (candidate := candidates_by_id.get(segment_id)) is not None
                    ],
                }
            )
            previous_result["analysis_plan"] = previous_plan

            def report_render(progress: int, message: str) -> None:
                _raise_if_cancel_requested(job_id)
                _update_live_edit_job(
                    job_id,
                    progress=progress,
                    phase="render",
                    task_name="영상 렌더링",
                    message=message,
                )

            try:
                _update_live_edit_job(
                    job_id,
                    status="running",
                    progress=0,
                    phase="render",
                    task_name="영상 렌더링",
                    message="사용자가 선택한 구간으로 편집을 준비하는 중입니다.",
                )
                pipeline = LiveEditPipeline(get_media_root())
                result = await asyncio.to_thread(
                    pipeline.rerender_from_selection,
                    job_id,
                    request.segment_ids,
                    plan=dict(previous_result.get("analysis_plan") or {}),
                    server_access_token=server_access_token,
                    progress_callback=report_render,
                    cancel_callback=lambda: _raise_if_cancel_requested(job_id),
                )
                final_plan = dict(previous_result.get("analysis_plan") or {})
                selected_ids = result.get("selected_segment_ids") or request.segment_ids
                candidates_by_id = {
                    str(item.get("segment_id")): item
                    for item in final_plan.get("candidates") or []
                    if isinstance(item, dict)
                }
                final_plan.update(
                    {
                        "clips": [
                            {
                                key: candidate[key]
                                for key in ("segment_id", "start", "end", "llm_score")
                                if key in candidate
                            }
                            for segment_id in selected_ids
                            if (candidate := candidates_by_id.get(str(segment_id)))
                            is not None
                        ],
                        "selected_segment_ids": selected_ids,
                        "rendered_filename": result.get("rendered_filename"),
                    }
                )
                # 실제 렌더링한 미세 컷을 원래 섹션 범위로 되돌리지 않는다.
                if "clips" in result:
                    final_plan["clips"] = result["clips"]
                for key in ("filler_summary", "filler_cuts", "filler_missing_timing"):
                    if key in result:
                        final_plan[key] = result[key]
                merged_result = {
                    **previous_result,
                    **result,
                    "analysis_plan": final_plan,
                    "awaiting_selection": False,
                }
                # 렌더링 성공은 로컬 완료의 기준이다. 서버 이력 동기화 실패가 이미
                # 생성된 결과 영상까지 폐기하게 해서는 안 된다.
                LocalJobStore(get_database_root()).save_completed(
                    job_id,
                    merged_result,
                    owner_id=LIVE_EDIT_JOBS.get(job_id, {}).get("owner_id"),
                )
                vod_url = str(previous_result.get("vod_url") or "")
                sync_warning = None
                if server_access_token and vod_url:
                    try:
                        vod_id = extract_video_id(vod_url)
                        server_job_id = await asyncio.to_thread(
                            create_server_job,
                            server_access_token,
                            client_job_id=job_id,
                            source_id=vod_id,
                            source_url=vod_url,
                        )
                        await asyncio.to_thread(
                            save_server_result,
                            server_access_token,
                            server_job_id,
                            merged_result,
                            selection={"selected_segment_ids": request.segment_ids},
                        )
                    except ServerJobError as exc:
                        sync_warning = str(exc)
                _update_live_edit_job(
                    job_id,
                    status="completed",
                    progress=100,
                    phase="render",
                    message=(
                        "선택한 구간으로 영상을 다시 만들었습니다."
                        if not sync_warning
                        else f"영상 생성은 완료됐지만 {sync_warning}"
                    ),
                    result=merged_result,
                    error=None,
                )
                _cleanup_edit_transient_state(job_id)
            except LiveEditCancelled:
                _update_live_edit_job(
                    job_id,
                    status="awaiting_selection",
                    progress=100,
                    phase="selection",
                    task_name="구간 선택",
                    message="렌더링을 취소했습니다. 선택 구간을 수정하거나 다시 렌더링할 수 있습니다.",
                    result=previous_result,
                    error=None,
                )
                _cleanup_edit_transient_state(job_id)
            except LiveEditPipelineError as exc:
                await _discard_failed_edit_job(job_id, exc)
            except Exception as exc:
                await _discard_failed_edit_job(job_id, exc)
    finally:
        EDIT_JOB_LOCKS.pop(job_id, None)


def _edit_output_dir(job_id: str) -> Path:
    if not job_id or Path(job_id).name != job_id:
        raise HTTPException(status_code=400, detail="잘못된 편집 작업 ID입니다.")
    directory = get_media_root() / "yt-edit" / job_id
    if not directory.exists() or not directory.is_dir():
        raise HTTPException(status_code=404, detail="편집 작업을 찾을 수 없습니다.")
    return directory


def _edit_media_response(output_dir: Path, plan: dict, kind: str):
    if kind == "source":
        media_path = Path(str(plan.get("source_video_path", "")))
        if not media_path.is_absolute():
            media_path = (Path.cwd() / media_path).resolve()
    elif kind == "rendered":
        filename = str(plan.get("rendered_filename") or "edited.mp4")
        if Path(filename).name != filename:
            raise HTTPException(status_code=404, detail="잘못된 영상 경로입니다.")
        media_path = output_dir / filename
    else:
        raise HTTPException(status_code=404, detail="지원하지 않는 영상 종류입니다.")
    if not media_path.exists() or not media_path.is_file():
        raise HTTPException(status_code=404, detail="영상 파일을 찾을 수 없습니다.")
    return FileResponse(
        media_path,
        media_type="video/mp4",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/youtube/edit/{job_id}/segments")
async def get_edit_segments(job_id: str, user=Depends(get_current_user)):
    _job_for_user(job_id, user)
    current = LIVE_EDIT_JOBS.get(job_id)
    plan = (current or {}).get("result", {}).get("analysis_plan")
    if not isinstance(plan, dict):
        raise HTTPException(
            status_code=404, detail="선택 대기 중인 분석 작업을 찾을 수 없습니다."
        )
    pipeline = LiveEditPipeline(get_media_root())
    try:
        return pipeline.get_segment_review(job_id, plan)
    except LiveEditPipelineError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.put("/api/youtube/edit/{job_id}/segments", status_code=202)
async def update_edit_segments(
    job_id: str,
    request: SegmentSelectionRequest,
    authorization: str | None = Header(default=None),
    user=Depends(get_current_user),
):
    _job_for_user(job_id, user)
    current = LIVE_EDIT_JOBS.get(job_id)
    if not current or not isinstance(
        (current.get("result") or {}).get("analysis_plan"), dict
    ):
        raise HTTPException(
            status_code=404, detail="선택 대기 중인 분석 작업을 찾을 수 없습니다."
        )
    if (
        current
        and current.get("phase") == "render"
        and current.get("status")
        in {
            "queued",
            "running",
        }
    ):
        raise HTTPException(
            status_code=409, detail="이미 선택 구간을 렌더링하고 있습니다."
        )
    if current is None:
        LIVE_EDIT_JOBS[job_id] = {
            "job_id": job_id,
            "status": "queued",
            "progress": 0,
            "phase": "render",
            "task_name": "영상 렌더링",
            "message": "선택 구간 렌더링을 준비하는 중입니다.",
        }
    else:
        current.update(
            {
                "status": "queued",
                "progress": 0,
                "phase": "render",
                "task_name": "영상 렌더링",
                "message": "선택 구간 렌더링을 준비하는 중입니다.",
                "error": None,
            }
        )
    if authorization:
        LIVE_EDIT_ACCESS_TOKENS[job_id] = authorization
    asyncio.create_task(_run_segment_selection_job(job_id, request, authorization))
    return LIVE_EDIT_JOBS[job_id]


@app.get("/api/youtube/edit/{job_id}/media/{kind}")
async def get_edit_media(job_id: str, kind: str, user=Depends(get_current_user)):
    _job_for_user(job_id, user)
    output_dir = _edit_output_dir(job_id)
    active = LIVE_EDIT_JOBS.get(job_id, {})
    result = active.get("result") or (_completed_job(job_id) or {}).get("result") or {}
    recipe_files = {
        "recipes-json": ("recipes.json", "application/json"),
        "recipes-markdown": ("recipes.md", "text/markdown; charset=utf-8"),
        "recipes-zip": ("recipes.zip", "application/zip"),
    }
    if kind in recipe_files:
        filename, media_type = recipe_files[kind]
        recipe_path = output_dir / filename
        if not recipe_path.is_file():
            raise HTTPException(status_code=404, detail="레시피 요약본이 없습니다. 요리 카테고리로 다시 분석하세요.")
        return FileResponse(recipe_path, media_type=media_type, filename=filename,
                            headers={"Cache-Control": "no-store"})
    if kind == "source":
        source = Path(
            str(
                (active.get("result") or {})
                .get("analysis_plan", {})
                .get("source_video_path", "")
            )
        )
        if source.is_file():
            return FileResponse(
                source, media_type="video/mp4", headers={"Cache-Control": "no-store"}
            )
    if kind != "rendered":
        raise HTTPException(
            status_code=404, detail="원본 미리보기 단계가 종료되었습니다."
        )
    filename = Path(str(result.get("rendered_filename") or "")).name
    if not filename:
        raise HTTPException(
            status_code=404, detail="완료된 결과 영상을 찾을 수 없습니다."
        )
    media_path = output_dir / filename
    if not media_path.is_file():
        raise HTTPException(status_code=404, detail="영상 파일을 찾을 수 없습니다.")
    return FileResponse(
        media_path, media_type="video/mp4", headers={"Cache-Control": "no-store"}
    )

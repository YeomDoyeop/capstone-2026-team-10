"""Supabase 인증과 분석 작업 저장소 연동."""

from __future__ import annotations

from datetime import datetime, timezone
from functools import lru_cache
from typing import Any

from app.config import get_supabase_anon_key, get_supabase_service_role_key, get_supabase_url

try:
    from supabase import Client, create_client
except ImportError:  # pragma: no cover - 설치 안내를 위한 방어 코드
    Client = Any
    create_client = None


def is_auth_configured() -> bool:
    return bool(get_supabase_url() and get_supabase_anon_key())


def is_storage_configured() -> bool:
    return bool(get_supabase_url() and get_supabase_service_role_key())


@lru_cache
def get_auth_client() -> Client:
    if create_client is None or not is_auth_configured():
        raise RuntimeError("Supabase 인증 설정이 없습니다.")
    return create_client(get_supabase_url(), get_supabase_anon_key())


@lru_cache
def get_service_client() -> Client:
    if create_client is None or not is_storage_configured():
        raise RuntimeError("Supabase 서버 설정이 없습니다.")
    return create_client(get_supabase_url(), get_supabase_service_role_key())


def create_job(user_id: str, values: dict[str, Any]) -> dict[str, Any]:
    response = get_service_client().table("analysis_jobs").insert({"user_id": user_id, **values}).execute()
    return response.data[0]


def get_job_by_client_id(user_id: str, client_job_id: str) -> dict[str, Any] | None:
    """클라이언트 작업 식별자에 연결된 분석 이력을 반환한다.

    ``analysis_jobs.user_id, client_job_id``의 고유 제약과 같은 조건을 사용한다.
    결과 동기화 재시도와 동시 생성 요청에서 동일한 서버 작업을 재사용하기 위한
    조회이므로 반드시 사용자 범위를 함께 제한한다.
    """
    response = (
        get_service_client()
        .table("analysis_jobs")
        .select("*")
        .eq("user_id", user_id)
        .eq("client_job_id", client_job_id)
        .limit(1)
        .execute()
    )
    return response.data[0] if response.data else None


def get_job(user_id: str, job_id: str) -> dict[str, Any] | None:
    response = get_service_client().table("analysis_jobs").select("*").eq("id", job_id).eq("user_id", user_id).execute()
    return response.data[0] if response.data else None


def upsert_result(user_id: str, job_id: str, values: dict[str, Any]) -> dict[str, Any] | None:
    if get_job(user_id, job_id) is None:
        return None
    response = get_service_client().table("analysis_results").upsert({"job_id": job_id, **values}, on_conflict="job_id").execute()
    return response.data[0] if response.data else None


def get_result(user_id: str, job_id: str) -> dict[str, Any] | None:
    if get_job(user_id, job_id) is None:
        return None
    response = get_service_client().table("analysis_results").select("*").eq("job_id", job_id).execute()
    return response.data[0] if response.data else None


def create_transcription_job(user_id: str, values: dict[str, Any]) -> dict[str, Any]:
    response = get_service_client().table("transcription_jobs").insert({"user_id": user_id, **values}).execute()
    return response.data[0]


def get_transcription_job(user_id: str, runpod_job_id: str) -> dict[str, Any] | None:
    response = get_service_client().table("transcription_jobs").select("*").eq("runpod_job_id", runpod_job_id).eq("user_id", user_id).execute()
    return response.data[0] if response.data else None


def get_active_transcription_job_by_client_id(user_id: str, client_job_id: str) -> dict[str, Any] | None:
    response = (
        get_service_client()
        .table("transcription_jobs")
        .select("*")
        .eq("user_id", user_id)
        .eq("client_job_id", client_job_id)
        .in_("status", ["queued", "in_progress", "cancel_requested"])
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    return response.data[0] if response.data else None


def request_transcription_cancel(user_id: str, client_job_id: str, expires_at: str) -> None:
    get_service_client().table("transcription_cancel_requests").upsert(
        {"user_id": user_id, "client_job_id": client_job_id, "expires_at": expires_at},
        on_conflict="user_id,client_job_id",
    ).execute()


def is_transcription_cancel_requested(user_id: str, client_job_id: str) -> bool:
    response = (
        get_service_client()
        .table("transcription_cancel_requests")
        .select("client_job_id")
        .eq("user_id", user_id)
        .eq("client_job_id", client_job_id)
        .gt("expires_at", datetime.now(timezone.utc).isoformat())
        .limit(1)
        .execute()
    )
    return bool(response.data)


def clear_transcription_cancel_request(user_id: str, client_job_id: str) -> None:
    get_service_client().table("transcription_cancel_requests").delete().eq("user_id", user_id).eq("client_job_id", client_job_id).execute()


def clear_expired_transcription_cancel_requests(cutoff: str) -> None:
    get_service_client().table("transcription_cancel_requests").delete().lt("expires_at", cutoff).execute()


def delete_transcription_job(user_id: str, runpod_job_id: str) -> None:
    get_service_client().table("transcription_jobs").delete().eq("user_id", user_id).eq("runpod_job_id", runpod_job_id).execute()


def update_transcription_job(user_id: str, runpod_job_id: str, values: dict[str, Any]) -> dict[str, Any] | None:
    updated_values = {**values, "updated_at": datetime.now(timezone.utc).isoformat()}
    response = get_service_client().table("transcription_jobs").update(updated_values).eq("runpod_job_id", runpod_job_id).eq("user_id", user_id).execute()
    return response.data[0] if response.data else None


def get_expired_transcription_jobs(cutoff: str) -> list[dict[str, Any]]:
    response = get_service_client().table("transcription_jobs").select("*").in_("status", ["queued", "in_progress", "cancel_requested"]).lt("lease_expires_at", cutoff).execute()
    return response.data or []


def get_expired_transcription_results(cutoff: str) -> list[dict[str, Any]]:
    response = get_service_client().table("transcription_jobs").select("*").eq("status", "completed").not_.is_("result_expires_at", "null").lt("result_expires_at", cutoff).execute()
    return response.data or []

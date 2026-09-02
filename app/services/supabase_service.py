"""Supabase 인증과 분석 작업 저장소 연동."""

from __future__ import annotations

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


def get_job(user_id: str, job_id: str) -> dict[str, Any] | None:
    response = get_service_client().table("analysis_jobs").select("*").eq("id", job_id).eq("user_id", user_id).execute()
    return response.data[0] if response.data else None


def update_job(user_id: str, job_id: str, values: dict[str, Any]) -> dict[str, Any] | None:
    response = get_service_client().table("analysis_jobs").update(values).eq("id", job_id).eq("user_id", user_id).execute()
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

"""AVE 서버에 로컬 편집 작업 이력과 분석 결과를 기록한다."""

from __future__ import annotations

from typing import Any

import requests

from app.config import get_ave_server_url


class ServerJobError(RuntimeError):
    pass


def create_job(access_token: str, *, client_job_id: str, source_id: str, source_url: str) -> str:
    payload = _request("POST", "/api/analysis-jobs", access_token, {"client_job_id": client_job_id, "source_id": source_id, "source_url": source_url})
    job_id = payload.get("id")
    if not isinstance(job_id, str):
        raise ServerJobError("AVE 서버가 작업 ID를 반환하지 않았습니다.")
    return job_id


def save_result(access_token: str, job_id: str, result: dict[str, Any], *, selection: dict[str, Any] | None = None) -> None:
    plan = result.get("analysis_plan") if isinstance(result.get("analysis_plan"), dict) else {}
    candidates = plan.get("candidates") if isinstance(plan.get("candidates"), list) else result.get("candidates") or []
    segments = [
        {
            "segment_index": index,
            "start_ms": int(float(item.get("start", 0)) * 1000),
            "end_ms": max(1, int(float(item.get("end", 0)) * 1000)),
            # 서버 이력에는 원본 전사문을 보내지 않는다.
            "text": "",
            "script_importance": _score(item.get("llm_score")),
            "comment_timestamp_count": None,
            "heatmap_score": None,
            "average_volume_dbfs": _number(item.get("average_volume_dbfs")),
            # 현재 선택 근거는 섹션 LLM 점수 하나뿐이다. 서버의 기존
            # final_score 필드는 별도 보정 점수가 아니라 이 값을 기록한다.
            "final_score": _score(item.get("llm_score")),
            "recommended": str(item.get("segment_id")) in set(plan.get("recommended_segment_ids") or result.get("recommended_segment_ids") or []),
        }
        for index, item in enumerate(candidates)
        if float(item.get("end", 0)) > float(item.get("start", 0))
    ]
    _request("PUT", f"/api/analysis-jobs/{job_id}/result", access_token, {"script": None, "segments": segments, "heatmap": [], "recommendation": {"recommended_segment_ids": plan.get("recommended_segment_ids") or result.get("recommended_segment_ids", [])}, "selection": selection or {}})


def _request(method: str, path: str, access_token: str, payload: dict[str, Any]) -> dict[str, Any]:
    base_url = get_ave_server_url()
    if not base_url.startswith("https://") or not access_token:
        raise ServerJobError("AVE_SERVER_URL과 로그인 토큰이 필요합니다.")
    try:
        response = requests.request(method, f"{base_url}{path}", headers={"Authorization": access_token, "Content-Type": "application/json"}, json=payload, timeout=30)
        response.raise_for_status()
        body = response.json()
    except requests.RequestException as exc:
        detail = ""
        response = getattr(exc, "response", None)
        if response is not None:
            try:
                detail = str(response.json().get("detail") or "")
            except (ValueError, AttributeError):
                detail = response.text.strip()[:500]
        suffix = f": {detail}" if detail else ""
        raise ServerJobError(f"AVE 서버와 작업 이력을 동기화하지 못했습니다{suffix}") from exc
    except ValueError as exc:
        raise ServerJobError("AVE 서버 응답 형식이 올바르지 않습니다.") from exc
    return body if isinstance(body, dict) else {}


def _number(value: object) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _integer(value: object) -> int | None:
    try:
        return max(0, int(value)) if value is not None else None
    except (TypeError, ValueError):
        return None


def _score(value: object) -> float | None:
    number = _number(value)
    if number is None:
        return None
    # The client analysis contract deliberately uses a detailed 0..1000
    # integer scale. The server history contract stores normalized values.
    # Clamp before division so malformed values cannot escape that contract.
    return min(1000.0, max(0.0, number)) / 1000.0

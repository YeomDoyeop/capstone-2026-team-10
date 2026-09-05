"""클라이언트와 서버가 교환하는 분석 데이터 모델."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


JobStatus = Literal["completed"]


class AuthUserResponse(BaseModel):
    id: str
    email: str | None = None


class AuthConfigResponse(BaseModel):
    supabase_url: str
    supabase_anon_key: str


class AnalysisJobCreateRequest(BaseModel):
    client_job_id: str | None = Field(default=None, min_length=1, max_length=100)
    source_id: str = Field(min_length=1, max_length=200)
    source_url: str | None = Field(default=None, max_length=2_000)
    title: str | None = Field(default=None, max_length=1_000)
    duration_ms: int | None = Field(default=None, ge=0)
class SegmentAnalysis(BaseModel):
    segment_index: int = Field(ge=0)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    text: str = Field(default="", max_length=50_000)
    script_importance: float | None = Field(default=None, ge=0, le=1)
    chat_density: float | None = Field(default=None, ge=0)
    comment_timestamp_count: int | None = Field(default=None, ge=0)
    heatmap_score: float | None = Field(default=None, ge=0)
    average_volume_dbfs: float | None = None
    final_score: float | None = Field(default=None, ge=0, le=1)
    recommended: bool | None = None


class AnalysisResultRequest(BaseModel):
    script: str | None = Field(default=None, max_length=2_000_000)
    segments: list[SegmentAnalysis] = Field(default_factory=list, max_length=10_000)
    heatmap: list[dict[str, Any]] = Field(default_factory=list, max_length=10_000)
    recommendation: dict[str, Any] = Field(default_factory=dict)
    selection: dict[str, Any] = Field(default_factory=dict)


class AnalysisJobResponse(BaseModel):
    id: str
    client_job_id: str
    status: JobStatus
    progress: int


class TemporaryAudioResponse(BaseModel):
    file_id: str
    public_url: str


class RemoteTranscriptionRequest(BaseModel):
    file_id: str = Field(min_length=32, max_length=32, pattern="^[a-f0-9]+$")
    language: str = Field(default="ko", min_length=1, max_length=20)
    initial_prompt: str | None = Field(default=None, max_length=1_000)
    hotwords: str | None = Field(default=None, max_length=1_000)
    speed: float = Field(default=1.0, ge=1.0, le=4.0)
    track_progress: bool = False
    client_job_id: str | None = Field(default=None, min_length=1, max_length=100)
    server_job_id: str | None = Field(default=None, max_length=100)


class RemoteTranscriptionStartResponse(BaseModel):
    job_id: str
    status: str
    progress: int
    message: str
    lease_expires_at: str | None = None


class RemoteTranscriptionStatusResponse(RemoteTranscriptionStartResponse):
    result: dict[str, Any] | None = None


class LLMGenerateRequest(BaseModel):
    provider: Literal["gemini", "deepseek"] = "deepseek"
    model: str | None = Field(default=None, max_length=200)
    system: str = Field(min_length=1, max_length=100_000)
    prompt: str = Field(min_length=1, max_length=2_000_000)
    response_schema: dict[str, Any] | None = None


class LLMGenerateResponse(BaseModel):
    text: str

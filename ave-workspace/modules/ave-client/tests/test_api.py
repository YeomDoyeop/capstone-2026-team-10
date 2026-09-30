import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.main import (
    EDIT_JOB_LOCKS,
    LIVE_EDIT_JOBS,
    METADATA_MATERIAL_JOBS,
    WHISPER_TRANSCRIPT_JOBS,
    METADATA_JOB_RETENTION,
    _cleanup_expired_metadata_jobs,
    _job_for_user,
    _run_segment_selection_job,
    app,
    get_current_user,
    get_youtube_metadata_material_job,
    index,
    score_live_edit,
    start_live_edit,
    update_edit_segments,
)
from app.schemas import (
    LiveEditRequest,
    SegmentSelectionRequest,
    WhisperPreparationRequest,
    YouTubeMetadataMaterialsRequest,
)
from app.services.local_job_store import LocalJobStore
from app.services.live_edit_pipeline import LiveEditCancelled


def test_frontend_index_is_served():
    response = asyncio.run(index())

    assert response.status_code == 200
    assert response.path.name == "index.html"


def test_target_duration_accepts_every_second_from_one_minute_to_two_hours():
    assert (
        LiveEditRequest(
            job_id="test-job",
            vod_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            target_duration_seconds=61,
        ).target_duration_seconds
        == 61
    )
    assert (
        LiveEditRequest(
            job_id="test-job",
            vod_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            target_duration_seconds=7200,
        ).target_duration_seconds
        == 7200
    )

    with pytest.raises(ValueError):
        LiveEditRequest(
            job_id="test-job",
            vod_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            target_duration_seconds=7201,
        )


def test_scoring_can_only_start_from_completed_structure(monkeypatch):
    job_id = "dQw4w9WgXcQ.structure"
    LIVE_EDIT_JOBS[job_id] = {
        "job_id": job_id,
        "owner_id": "user-1",
        "status": "awaiting_scoring",
        "request": LiveEditRequest(
            job_id=job_id,
            vod_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        ).model_dump(),
    }

    def discard_task(coroutine):
        coroutine.close()

    monkeypatch.setattr("app.main.asyncio.create_task", discard_task)
    result = asyncio.run(
        score_live_edit(job_id, "Bearer session", {"id": "user-1"}, 300)
    )

    assert result["status"] == "queued"
    assert result["stop_after_structure"] is False
    assert result["request"]["target_duration_seconds"] == 300
    LIVE_EDIT_JOBS.pop(job_id, None)


def test_whisper_auto_language_is_represented_by_an_omitted_language_hint():
    request = LiveEditRequest(
        job_id="test-job",
        vod_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        transcription_source="whisper_api",
        stt_language=None,
    )
    assert request.stt_language is None


def test_metadata_and_whisper_requests_accept_the_same_workflow_job_id():
    url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    job_id = "dQw4w9WgXcQ.123456789abc"

    assert YouTubeMetadataMaterialsRequest(url=url, job_id=job_id).job_id == job_id
    assert WhisperPreparationRequest(url=url, job_id=job_id).job_id == job_id


def test_workflow_uses_current_endpoints_and_restored_options():
    source = (
        Path(__file__).resolve().parents[1] / "ui" / "src" / "WorkflowApp.tsx"
    ).read_text(encoding="utf-8")
    styles = (
        Path(__file__).resolve().parents[1] / "ui" / "src" / "WorkflowApp.css"
    ).read_text(encoding="utf-8")
    compact_source = " ".join(source.split())

    for value in (
        "/api/youtube/metadata",
        "/api/youtube/metadata/whisper-settings",
        "/api/youtube/metadata/whisper-transcript",
        "/api/youtube/metadata/whisper-transcript/start",
        "/api/youtube/edit/script",
        "/api/youtube/edit/start",
        "/api/youtube/edit/${job.job_id}/score",
        "transcript_language",
        "stt_language",
        "stt_initial_prompt",
        "stt_hotwords",
        "stt_speed",
    ):
        assert value in source
    assert "interactive_selection" not in source
    assert "/subtitles" not in source
    assert "subtitle_offset_seconds" not in source
    assert "scriptSourceOptions" in source
    assert "스크립트 문장" in source
    assert "onClick={startAnalysis}" in source and "> 분할 </button>" in compact_source
    assert "다음: 챕터 내 필수 관계 판별" in source
    assert 'className="script-workflow-card"' in source
    assert 'className="script-workflow-card scored-workflow-card"' in source
    assert "onBlur={commitTargetDurationInput}" in source
    assert 'chapter_split_mode: "automatic"' in source
    assert 'label: "업로더 기준"' in source
    assert 'label: "자동 분할"' in source
    assert ".scored-workflow-card .review-layout > .preview-card { position: sticky;" in styles
    options_start = source.index("const scriptSourceOptions")
    options_end = source.index("function transitionToPhase", options_start)
    options = source[options_start:options_end]
    assert "whisperActive" in options
    assert (
        options.index("whisper_api")
        < options.index("youtube_subtitle")
        < options.index("youtube_caption")
    )
    assert "review.segments" not in source
    assert "총점 {section.final_score.toFixed(2)}" in source
    assert ".review-layout .final-score-badge" in styles
    assert 'className="score-badge chapter-score-badge"' in source
    assert ".review-layout .chapter-score-badge" in styles
    assert 'className="score-badge"' in source
    assert "댓글 {section.comment_score.toFixed(2)}" in source
    assert 'typeof section.comment_score === "number"' in source
    assert ".review-layout .comment-score-badge" in styles
    assert "히트맵 {section.heatmap_score.toFixed(2)}" in source
    assert ".review-layout .heatmap-score-badge" in styles
    assert "음량 {section.volume_score.toFixed(2)}" in source
    assert ".review-layout .volume-score-badge" in styles
    assert "채팅 {section.chat_score.toFixed(2)}" in source
    assert ".review-layout .chat-score-badge" in styles
    llm_badge = source.index("LLM {section.llm_score.toFixed(2)}")
    heatmap_badge = source.index("히트맵 {section.heatmap_score.toFixed(2)}")
    comment_badge = source.index("댓글 {section.comment_score.toFixed(2)}")
    volume_badge = source.index("음량 {section.volume_score.toFixed(2)}")
    chat_badge = source.index("채팅 {section.chat_score.toFixed(2)}")
    final_badge = source.index("총점 {section.final_score.toFixed(2)}")
    assert (
        final_badge
        < heatmap_badge
        < comment_badge
        < llm_badge
        < chat_badge
        < volume_badge
    )
    assert 'className="score-separator"' in source
    assert ">◀</span>" in source
    assert 'className="segment-scores"' in source
    assert "use_timestamp_comments: materialSelections.comments" in source
    assert "use_chat_score: materialSelections.chat" in source
    assert (
        "Math.round((Number(body.duration_seconds) || 0) / 4 / 30) * 30"
        in compact_source
    )
    assert "초기값은 원본 영상 길이의 1/4입니다." not in source
    assert "button:disabled { cursor: default;" in styles
    assert (
        ':disabled, [aria-disabled="true"], .disabled, .disabled * { cursor: default !important; }'
        in styles
    )
    assert ".whisper-activation * { cursor: default; }" in styles
    assert "activeProgress.log" in source
    assert 'className="progress-log"' in source
    assert "progress-log-pan" in styles
    assert "mask-image: linear-gradient(to right" not in styles
    assert "function transitionToPhase" in source
    assert "runTransition" not in source
    assert "async function advanceToAnalysis()" in source
    advance_start = source.index("async function advanceToAnalysis()")
    advance_end = source.index("async function startAnalysis()", advance_start)
    advance = source[advance_start:advance_end]
    assert "prepareWhisperTranscript()" in advance
    assert "whisper-transcript/start" in source
    assert "cancelWhisperTranscript" in source
    assert "whisperTranscribing" in source
    assert "const [workflowJobId, setWorkflowJobId]" in source
    assert "setWorkflowJobId(started.job_id)" in source
    assert "job_id: workflowJobId, url: url.trim()" in compact_source
    assert "job_id: workflowJobId, vod_url: url.trim()" in compact_source
    assert 'ariaLabel="LLM 엔진"' in source
    assert "!materialSelections.subtitles && !materialSelections.captions" in source
    assert "const whisperActive = whisperEnabled || whisperRequired" in source
    assert "setWhisperEnabled(true)" in source
    assert "requiresWhisper || Boolean(body.default_whisper_cache_available)" in source
    assert ': "whisper_api"' in source
    assert "controlsLocked || whisperRequired" in source
    assert 'artifacts[0]?.kind || (whisperRequired ? "whisper" : null)' in source
    assert "{whisperEnabled && (" not in source
    assert source.count(">\n        Whisper STT\n      </button>") == 1
    assert 'job?.status === "paused" ? (' in source
    assert 'whisperTranscript?.status === "paused" ? (' in source
    assert "onClick={retryWhisperTranscript}" in source
    assert 'if (current.status === "paused") {' in source
    assert (
        'if (await prepareWhisperTranscript()) transitionToPhase("review")' in source
    )
    assert '{phase === "review" && !review && (' in source
    assert "{!structuredChapters.length && (" in source
    assert 'onClick={() => changePhase("analysis")}' not in source
    assert ">\n            재시도\n          </button>" in source
    assert ">\n            분석 재개\n          </button>" not in source
    assert "disabled={busy}" in source and 'setSetting("llm_provider", value)' in source
    assert (
        "materialSelections.subtitles" in options
        and "materialSelections.captions" in options
    )
    assert '"현재 작업을 중단하고 로그아웃하시겠습니까?"' in source
    assert "disabled={busy || !metadataReady || !token}" in source
    assert 'if (token || phase === "metadata") return;' in source
    assert (
        'id="youtube-url"' in source
        and 'type="url"' in source
        and "value={url}" in source
    )
    assert (
        ".thumbnail-viewer { position: sticky; top: 20px; align-self: start; }"
        in styles
    )
    assert (
        "@media (max-width: 1024px) { .thumbnail-viewer { position: static; } }"
        in styles
    )
    whisper_toggle_start = source.index("const updateWhisperEnabled")
    whisper_toggle_end = source.index("const setWhisperLanguage", whisper_toggle_start)
    whisper_toggle = source[whisper_toggle_start:whisper_toggle_end]
    assert "prepareWhisperSettings" not in whisper_toggle
    assert (
        "const [whisperPromptEnabled, setWhisperPromptEnabled] = useState(false)"
        in source
    )
    assert (
        "const [whisperHotwordsEnabled, setWhisperHotwordsState] = useState(false)"
        in source
    )
    hotword_toggle_start = source.index("const setWhisperHotwordsEnabled")
    hotword_toggle_end = source.index(
        "const resetWhisperHotwords", hotword_toggle_start
    )
    hotword_toggle = source[hotword_toggle_start:hotword_toggle_end]
    assert "await prepareWhisperSettings()" in hotword_toggle
    assert "if (!enabled || whisperSuggestion) return" in hotword_toggle
    assert "onAdvanceToAnalysis={advanceToAnalysis}" in source
    assert "onClick={onAdvanceToAnalysis}" in source
    assert 'placeholder="쉼표로 구분"' not in source
    assert "resetWhisperSetting(" not in source
    assert "resetWhisperHotwords" in source
    assert 'htmlFor="whisper-hotwords-enabled"' in source
    assert 'id="whisper-hotwords-enabled"' in source
    assert "event.preventDefault();" in source
    assert "event.stopPropagation();" in source
    assert 'aria-label="핫워드 사용"' in source
    assert "setWhisperHotwordsEnabled(event.target.checked)" in compact_source
    assert "whisperHotwordsEnabled && ( <textarea" in compact_source
    assert 'whisperHotwordsEnabled ? settings.stt_hotwords : ""' in compact_source
    assert ".setting-card-actions" in styles
    assert 'aria-label="초기 프롬프트 사용"' in source
    assert "whisperPromptEnabled && ( <textarea" in compact_source
    assert "fixed-whisper-prompt" in source
    assert (
        'enabled ? fixedWhisperPrompts[current.stt_language] || "" : ""'
        in compact_source
    )
    assert ".fixed-whisper-prompt:disabled" in styles
    assert "function DetailedTime" in source
    assert "formatMilliseconds" in source
    assert 'addEventListener("seeked"' in source
    assert 'className="header-separator"' in source
    assert "Whisper STT (고품질 음성 인식)" not in source
    assert "STT(Speech-to-Text)에 사용할 언어와 처리 옵션을 설정합니다." in source
    assert (
        "원본 음성을 빠르게 재생할수록 처리 시간은 줄지만 인식 정확도가 낮아질 수 있습니다."
        in compact_source
    )
    assert ".heatmap { box-sizing: border-box;" in styles
    assert "border: 1px solid var(--ave-line);" in styles
    assert ".heatmap::after" in styles
    assert "clip-path: inset(1px);" in styles


def test_removed_legacy_routes_are_not_registered():
    paths = {route.path for route in app.routes}

    assert "/api/videos/upload" not in paths
    assert "/api/youtube/live/inspect" not in paths
    assert "/api/youtube/edit" not in paths


def test_user_job_routes_require_authenticated_user_dependency():
    protected_paths = {
        "/api/youtube/metadata/materials/start",
        "/api/youtube/metadata/whisper-settings",
        "/api/youtube/metadata/whisper-transcript/start",
        "/api/youtube/metadata/whisper-transcript/{job_id}",
        "/api/youtube/metadata/whisper-transcript/{job_id}/cancel",
        "/api/youtube/metadata/materials/{job_id}",
        "/api/youtube/edit/start",
        "/api/youtube/edit/status/{job_id}",
        "/api/youtube/edit/{job_id}/events",
        "/api/youtube/edit/active",
        "/api/youtube/edit/{job_id}/cancel",
        "/api/youtube/edit/cancel-active",
        "/api/youtube/edit/{job_id}/segments",
        "/api/youtube/edit/{job_id}/media/{kind}",
    }
    for route in app.routes:
        if route.path in protected_paths:
            assert any(
                dependency.call is get_current_user
                for dependency in route.dependant.dependencies
            )


def test_job_access_is_scoped_to_its_owner():
    job_id = "owned-job"
    LIVE_EDIT_JOBS[job_id] = {"job_id": job_id, "owner_id": "owner"}
    assert _job_for_user(job_id, {"id": "owner"})["job_id"] == job_id
    with pytest.raises(HTTPException, match="찾을 수 없습니다"):
        _job_for_user(job_id, {"id": "other"})
    LIVE_EDIT_JOBS.pop(job_id, None)


def test_expired_metadata_material_jobs_are_removed_without_polling():
    job_id = "expired-materials"
    METADATA_MATERIAL_JOBS[job_id] = {
        "job_id": job_id,
        "status": "completed",
        "completed_at": datetime.now(timezone.utc)
        - METADATA_JOB_RETENTION
        - timedelta(seconds=1),
    }
    _cleanup_expired_metadata_jobs()
    assert job_id not in METADATA_MATERIAL_JOBS


def test_expired_whisper_transcript_jobs_are_removed_without_polling():
    job_id = "expired-whisper"
    WHISPER_TRANSCRIPT_JOBS[job_id] = {
        "job_id": job_id,
        "status": "completed",
        "completed_at": datetime.now(timezone.utc)
        - METADATA_JOB_RETENTION
        - timedelta(seconds=1),
    }
    _cleanup_expired_metadata_jobs()
    assert job_id not in WHISPER_TRANSCRIPT_JOBS


def test_metadata_material_terminal_status_is_consumed_once():
    job_id = "metadata-terminal"
    user = {"id": "user-1"}
    METADATA_MATERIAL_JOBS[job_id] = {
        "job_id": job_id,
        "owner_id": "user-1",
        "status": "completed",
        "result": {"video_id": "dQw4w9WgXcQ"},
    }
    response = asyncio.run(get_youtube_metadata_material_job(job_id, user))

    assert response["status"] == "completed"
    assert job_id not in METADATA_MATERIAL_JOBS
    with pytest.raises(HTTPException, match="찾을 수 없습니다"):
        asyncio.run(get_youtube_metadata_material_job(job_id))


def test_edit_request_defaults_match_workflow_defaults():
    request = LiveEditRequest(
        job_id="defaults-job", vod_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    )

    assert request.llm_provider == "deepseek"
    assert request.transcription_source == "youtube_caption"
    assert request.transcript_language is None


def test_selection_api_starts_render_with_only_segment_ids(monkeypatch):
    job_id = "selection-contract"
    LIVE_EDIT_JOBS[job_id] = {
        "job_id": job_id,
        "owner_id": "user-1",
        "status": "awaiting_selection",
        "phase": "selection",
        "result": {
            "analysis_plan": {
                "candidates": [{"segment_id": "chapter-00", "start": 0, "end": 10}]
            }
        },
    }
    created = []

    def no_background_task(coroutine):
        created.append(coroutine)
        coroutine.close()

    monkeypatch.setattr("app.main.asyncio.create_task", no_background_task)
    response = asyncio.run(
        update_edit_segments(
            job_id,
            SegmentSelectionRequest(segment_ids=["chapter-00"]),
            user={"id": "user-1"},
        )
    )

    assert response["phase"] == "render"
    assert len(created) == 1
    LIVE_EDIT_JOBS.pop(job_id, None)


def test_analysis_start_api_queues_only_memory_job(monkeypatch):
    created = []
    monkeypatch.setattr(
        "app.main.get_video_metadata",
        lambda *_args, **_kwargs: {
            "subtitles_available": True,
            "captions_available": False,
        },
    )

    async def immediate_to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr("app.main.asyncio.to_thread", immediate_to_thread)

    def no_background_task(coroutine):
        created.append(coroutine)
        coroutine.close()

    monkeypatch.setattr("app.main.asyncio.create_task", no_background_task)
    response = asyncio.run(
        start_live_edit(
            LiveEditRequest(
                job_id="analysis-job",
                vod_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            ),
            authorization="Bearer session",
            user={"id": "user-1"},
        )
    )

    assert response["status"] == "queued"
    assert response["job_id"] in LIVE_EDIT_JOBS
    assert len(created) == 1
    LIVE_EDIT_JOBS.pop(response["job_id"], None)


def test_analysis_can_restart_same_job_after_returning_from_result(monkeypatch):
    job_id = "analysis-restart"
    owner_id = "user-1"
    vod_url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    LIVE_EDIT_JOBS[job_id] = {
        "job_id": job_id,
        "owner_id": owner_id,
        "status": "awaiting_selection",
        "phase": "selection",
        "result": {"analysis_plan": {}},
    }
    WHISPER_TRANSCRIPT_JOBS[job_id] = {
        "job_id": job_id,
        "owner_id": owner_id,
        "source_url": vod_url,
        "status": "completed",
    }
    created = []
    monkeypatch.setattr(
        "app.main.get_video_metadata",
        lambda *_args, **_kwargs: {
            "subtitles_available": True,
            "captions_available": False,
        },
    )

    async def immediate_to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr("app.main.asyncio.to_thread", immediate_to_thread)

    def no_background_task(coroutine):
        created.append(coroutine)
        coroutine.close()

    monkeypatch.setattr("app.main.asyncio.create_task", no_background_task)
    response = asyncio.run(
        start_live_edit(
            LiveEditRequest(
                job_id=job_id, vod_url=vod_url, transcription_source="whisper_api"
            ),
            authorization="Bearer session",
            user={"id": owner_id},
        )
    )

    assert response["status"] == "queued"
    assert response["phase"] == "analysis"
    assert len(created) == 1
    LIVE_EDIT_JOBS.pop(job_id, None)
    WHISPER_TRANSCRIPT_JOBS.pop(job_id, None)


def test_analysis_forces_prepared_whisper_when_no_youtube_script_exists(monkeypatch):
    job_id = "forced-whisper"
    owner_id = "user-1"
    vod_url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    WHISPER_TRANSCRIPT_JOBS[job_id] = {
        "job_id": job_id,
        "owner_id": owner_id,
        "source_url": vod_url,
        "status": "completed",
    }
    created = []

    def no_background_task(coroutine):
        created.append(coroutine)
        coroutine.close()

    monkeypatch.setattr(
        "app.main.get_video_metadata",
        lambda *_args, **_kwargs: {
            "subtitles_available": False,
            "captions_available": False,
        },
    )

    async def immediate_to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr("app.main.asyncio.to_thread", immediate_to_thread)
    monkeypatch.setattr("app.main.asyncio.create_task", no_background_task)
    response = asyncio.run(
        start_live_edit(
            LiveEditRequest(
                job_id=job_id, vod_url=vod_url, transcription_source="youtube_caption"
            ),
            authorization="Bearer session",
            user={"id": owner_id},
        )
    )

    assert response["transcription_source"] == "whisper_api"
    assert response["request"]["transcription_source"] == "whisper_api"
    assert len(created) == 1
    LIVE_EDIT_JOBS.pop(job_id, None)
    WHISPER_TRANSCRIPT_JOBS.pop(job_id, None)


def test_selection_render_persists_clips_and_sends_them_to_server(
    monkeypatch, tmp_path
):
    job_id = "selection-persistence"
    owner_id = "user-1"
    candidates = [
        {"segment_id": "section-a", "start": 10.0, "end": 20.0, "llm_score": 0.75},
        {"segment_id": "section-b", "start": 30.0, "end": 42.0, "llm_score": 0.92},
    ]
    LIVE_EDIT_JOBS[job_id] = {
        "job_id": job_id,
        "owner_id": owner_id,
        "status": "awaiting_selection",
        "phase": "selection",
        "result": {
            "vod_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "analysis_plan": {
                "candidates": candidates,
                "selected_segment_ids": ["section-a"],
                "clips": [candidates[0]],
            },
        },
    }

    class FakePipeline:
        def __init__(self, _media_root):
            pass

        def rerender_from_selection(
            self,
            _job_id,
            segment_ids,
            *,
            plan,
            progress_callback,
            cancel_callback,
            server_access_token,
        ):
            assert server_access_token == "Bearer session"
            cancel_callback()
            progress_callback(100, "렌더링 완료")
            return {
                "selected_segment_ids": list(segment_ids),
                "selected_duration_seconds": 21.8,
                "rendered_filename": "edited.mp4",
                "clips": [
                    {"segment_id": "section-a", "start": 10.0, "end": 15.0, "llm_score": 0.75},
                    {"segment_id": "section-a", "start": 15.2, "end": 20.0, "llm_score": 0.75},
                    {"segment_id": "section-b", "start": 30.0, "end": 42.0, "llm_score": 0.92},
                ],
            }

    synced: list[dict] = []

    async def run_direct(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr("app.main.LiveEditPipeline", FakePipeline)
    monkeypatch.setattr("app.main.asyncio.to_thread", run_direct)
    monkeypatch.setattr("app.main.get_media_root", lambda: tmp_path)
    monkeypatch.setattr(
        "app.main.create_server_job", lambda *_args, **_kwargs: "server-job"
    )
    monkeypatch.setattr(
        "app.main.save_server_result",
        lambda _token, _server_job_id, result, *, selection: synced.append(
            {"plan": result["analysis_plan"], "selection": selection}
        ),
    )

    asyncio.run(
        _run_segment_selection_job(
            job_id,
            SegmentSelectionRequest(segment_ids=["section-b", "section-a"]),
            "Bearer session",
        )
    )

    expected = [
        {"segment_id": "section-a", "start": 10.0, "end": 15.0, "llm_score": 0.75},
        {"segment_id": "section-a", "start": 15.2, "end": 20.0, "llm_score": 0.75},
        {"segment_id": "section-b", "start": 30.0, "end": 42.0, "llm_score": 0.92},
    ]
    assert LIVE_EDIT_JOBS[job_id]["result"]["analysis_plan"]["clips"] == expected
    stored = LocalJobStore(tmp_path / "db").get_completed(job_id)
    assert stored is not None
    assert stored["owner_id"] == owner_id
    assert stored["analysis_plan"]["clips"] == expected
    assert synced == [
        {
            "plan": {**LIVE_EDIT_JOBS[job_id]["result"]["analysis_plan"]},
            "selection": {"selected_segment_ids": ["section-b", "section-a"]},
        }
    ]
    assert job_id not in EDIT_JOB_LOCKS
    LIVE_EDIT_JOBS.pop(job_id, None)


def test_selection_render_cancel_preserves_the_users_latest_selection(
    monkeypatch, tmp_path
):
    job_id = "selection-cancel"
    candidates = [
        {"segment_id": "ai-choice", "start": 0.0, "end": 10.0, "llm_score": 0.9},
        {"segment_id": "user-choice", "start": 20.0, "end": 35.0, "llm_score": 0.5},
    ]
    LIVE_EDIT_JOBS[job_id] = {
        "job_id": job_id,
        "owner_id": "user-1",
        "status": "awaiting_selection",
        "phase": "selection",
        "result": {
            "analysis_plan": {
                "candidates": candidates,
                "recommended_segment_ids": ["ai-choice"],
                "selected_segment_ids": ["ai-choice"],
                "clips": [candidates[0]],
            }
        },
    }

    class CancelledPipeline:
        def __init__(self, _media_root):
            pass

        def rerender_from_selection(self, *_args, **_kwargs):
            raise LiveEditCancelled()

    async def run_direct(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr("app.main.LiveEditPipeline", CancelledPipeline)
    monkeypatch.setattr("app.main.asyncio.to_thread", run_direct)
    monkeypatch.setattr("app.main.get_media_root", lambda: tmp_path)

    asyncio.run(
        _run_segment_selection_job(
            job_id,
            SegmentSelectionRequest(segment_ids=["user-choice"]),
        )
    )

    job = LIVE_EDIT_JOBS[job_id]
    assert job["status"] == "awaiting_selection"
    assert job["result"]["analysis_plan"]["selected_segment_ids"] == [
        "user-choice"
    ]
    assert job["result"]["analysis_plan"]["recommended_segment_ids"] == [
        "ai-choice"
    ]
    assert job["result"]["analysis_plan"]["clips"] == [candidates[1]]
    LIVE_EDIT_JOBS.pop(job_id, None)

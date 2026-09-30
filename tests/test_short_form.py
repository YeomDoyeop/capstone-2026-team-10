import asyncio
import json

import pytest

from app.schemas import LiveEditRequest
from app.services.edit_policy import SHORT_FORM_SCORE_WEIGHTS
from app.services.llm_analysis_service import LLMAnalysisService
from app.services.live_edit_pipeline import (
    _apply_final_scores, _select_coherent_clips, LiveEditPipeline, LiveEditPipelineError,
)
from app.services.prompt_store import user_prompt, list_user_prompts


@pytest.mark.parametrize("profile", ["game", "variety"])
def test_short_form_request_default_and_bounds(profile):
    base = {"job_id": "test", "vod_url": "https://youtube.com/watch?v=example", "criteria_prompt": profile}
    assert LiveEditRequest(**base).target_duration_seconds == 120
    for requested, expected in [(60, 60), (123, 123), (180, 180), (600, 180)]:
        assert LiveEditRequest(**base, target_duration_seconds=requested).target_duration_seconds == expected
    assert LiveEditRequest(**{**base, "criteria_prompt": "ai_news"}).target_duration_seconds == 600


@pytest.mark.parametrize("profile", ["game", "variety"])
def test_heatmap_has_highest_weight_and_missing_values_are_renormalized(profile):
    rows = [{key: (1 if key == "heatmap_score" else 0) for key in SHORT_FORM_SCORE_WEIGHTS},
            {key: (0 if key == "heatmap_score" else 1) for key in SHORT_FORM_SCORE_WEIGHTS}]
    _apply_final_scores(rows, profile)
    assert [r["final_score"] for r in rows] == [0.55, 0.45]
    missing = [{"llm_score": 0.8, "chapter_llm_score": 0.5}]
    _apply_final_scores(missing, profile)
    assert missing[0]["final_score"] == pytest.approx(0.7)
    assert sum(SHORT_FORM_SCORE_WEIGHTS.values()) == pytest.approx(1)


def section(i, start, duration, score):
    return {"segment_id": str(i), "chapter_id": str(i // 3), "text": f"문장 {i}",
            "start": start, "end": start + duration, "final_score": score}


def test_short_form_keeps_bridge_sections_and_cross_chapter_context():
    class Analysis:
        def required_anchor_links(self, anchor, summary, rows, **kwargs):
            assert anchor == "2"
            assert kwargs["criteria_prompt"] == "variety"
            assert "5" in {r["id"] for r in rows}
            return ["0", "5"]

    rows = [section(i, i * 10, 10, 1 if i == 2 else 0.1) for i in range(9)]
    result, _ = _select_coherent_clips(rows, 60, Analysis(), criteria_prompt="variety")
    assert [r["segment_id"] for r in result] == [str(i) for i in range(6)]


def test_short_form_adds_immediate_neighbors_even_without_llm_links():
    class Analysis:
        def required_anchor_links(self, *args, **kwargs):
            return []

    rows = [section(i, i * 25, 25, 1 if i == 1 else 0.1) for i in range(3)]
    result, _ = _select_coherent_clips(rows, 60, Analysis(), criteria_prompt="game")
    assert result == rows  # 목표 60초보다 길지만 문맥을 보존한 75초


def test_oversized_bundle_is_skipped_and_other_scene_can_be_selected():
    class Analysis:
        def required_anchor_links(self, anchor, *args, **kwargs):
            return ["0", "2"] if anchor == "1" else []

    rows = [section(0, 0, 70, .1), section(1, 70, 70, 1), section(2, 140, 70, .1),
            section(3, 400, 60, .9), section(4, 460, 60, .8)]
    result, _ = _select_coherent_clips(rows, 120, Analysis(), criteria_prompt="game")
    assert [r["segment_id"] for r in result] == ["3", "4"]


def test_selected_ids_are_not_duplicated_and_total_never_exceeds_three_minutes():
    class Analysis:
        def required_anchor_links(self, *args, **kwargs):
            return []

    rows = [section(i, i * 25, 25, 1 - i / 100) for i in range(20)]
    result, _ = _select_coherent_clips(rows, 600, Analysis(), criteria_prompt="game")
    assert 60 <= sum(r["end"] - r["start"] for r in result) <= 180
    assert len(result) == len({r["segment_id"] for r in result})


def test_short_source_is_not_repeated_to_reach_one_minute():
    class Analysis:
        def required_anchor_links(self, *args, **kwargs):
            return []

    rows = [section(0, 0, 20, 1)]
    assert _select_coherent_clips(rows, 120, Analysis(), criteria_prompt="variety")[0] == rows


def test_short_form_link_prompt_and_profile_reach_llm(monkeypatch):
    agent = object.__new__(LLMAnalysisService)

    def request(system, prompt, **kwargs):
        data = json.loads(prompt)
        assert "예능·게임 1~3분" in system
        assert data["criteria_profile"] == user_prompt("variety")
        assert data["sections"][0]["start"] == 0
        return kwargs["validator"]({"indexes": [0, 2]})

    monkeypatch.setattr(agent, "_request_json", request)
    rows = [{"id": str(i), "text": "장면", "start": i * 10, "end": (i + 1) * 10} for i in range(3)]
    assert agent.required_anchor_links("1", "요약", rows, criteria_prompt="variety") == ["0", "2"]
    assert "variety" in {p["id"] for p in list_user_prompts()}


def test_scoring_query_cannot_bypass_short_form_limit(monkeypatch):
    from app.main import LIVE_EDIT_JOBS, score_live_edit
    request = LiveEditRequest(job_id="short-test", vod_url="https://example.com", criteria_prompt="variety")
    LIVE_EDIT_JOBS["short-test"] = {"owner_id": "owner", "status": "awaiting_scoring", "request": request.model_dump()}
    monkeypatch.setattr("app.main.asyncio.create_task", lambda task: task.close())
    try:
        result = asyncio.run(score_live_edit("short-test", "Bearer test", {"id": "owner"}, 600))
        assert result["request"]["target_duration_seconds"] == 180
    finally:
        LIVE_EDIT_JOBS.pop("short-test", None)


def test_manual_render_rejects_more_than_three_minutes(tmp_path):
    source = tmp_path / "source.mp4"
    source.touch()
    plan = {"criteria_prompt": "game", "source_video_path": str(source),
            "candidates": [section(0, 0, 181, 1)]}
    with pytest.raises(LiveEditPipelineError, match="최대 3분"):
        LiveEditPipeline(tmp_path).rerender_from_selection("test", ["0"], plan=plan)

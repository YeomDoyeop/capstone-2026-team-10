import json
import threading
import time

import pytest

from app.services import live_edit_pipeline
from app.services.live_edit_pipeline import CHAT_REACTION_OFFSET_SECONDS, SECTION_SCORE_WEIGHTS, LiveEditPipeline, LiveEditPipelineError, SUBTITLE_FONT_SIZE, SUBTITLE_LINE_WIDTH, SUBTITLE_MARGIN_BOTTOM, SUBTITLE_MIN_DURATION_SECONDS, WHISPER_LONG_SEGMENT_CHARACTERS, WHISPER_PRIORITY_GAP_SECONDS, _adjust_chat_timestamp, _apply_final_scores, _apply_heatmap_scores, _apply_point_scores, _apply_timestamp_comment_scores, _chat_score_points, _comment_timestamp_seconds, _hardware_decoding_args, _heatmap_section_score, _select_clips, _select_coherent_clips, _split_whisper_segment, _split_whisper_segments_parallel, _time_seconds, _video_encoding_args, _volume_score_points, _whisper_transcript_path, _write_json_atomic, fixed_whisper_initial_prompt, write_selected_subtitles
from app.services.live_youtube_service import LiveYouTubeError, load_prepared_transcript


def test_prepared_transcript_numeric_seconds_are_accepted():
    assert _time_seconds(1.25) == 1.25
    assert _time_seconds("1.25") == 1.25
    assert _time_seconds("01:02.5") == 62.5


def test_fixed_whisper_prompt_follows_known_language_without_content_context():
    assert "，" in fixed_whisper_initial_prompt("zh")
    assert "," in fixed_whisper_initial_prompt("en")
    assert "," in fixed_whisper_initial_prompt("ko")
    assert fixed_whisper_initial_prompt(None) == ""


def test_default_rendered_subtitle_style_and_short_duration_filter(tmp_path):
    output = tmp_path / "subtitles.srt"
    count = write_selected_subtitles(
        [
            {"start": 1.0, "end": 1.01, "text": "순간 오류"},
            {"start": 1.1, "end": 2.0, "text": "하나 둘 셋 넷 다섯 여섯 일곱 여덟 아홉 열 하나 둘 셋 넷 다섯 여섯"},
        ],
        [{"start": 0.0, "end": 2.0}],
        output,
    )

    assert (SUBTITLE_FONT_SIZE, SUBTITLE_MARGIN_BOTTOM, SUBTITLE_LINE_WIDTH) == (16, 12, 30)
    assert SUBTITLE_MIN_DURATION_SECONDS == 0.08
    assert count == 1
    content = output.read_text(encoding="utf-8")
    assert "순간 오류" not in content
    subtitle_lines = content.splitlines()[2:]
    assert len(subtitle_lines) > 1
    assert max(map(len, subtitle_lines)) <= SUBTITLE_LINE_WIDTH


def test_whisper_transcript_path_is_scoped_to_job(tmp_path):
    assert _whisper_transcript_path(tmp_path, "edit-job") == (
        tmp_path / "yt-edit" / "edit-job" / "edit-job.whisper-transcript.json"
    )


def test_long_whisper_segment_uses_midpoint_between_adjacent_words():
    class Analysis:
        def split_subtitle_words(self, _words, target_count, **_kwargs):
            assert target_count == 2
            return [{"start_word": 0, "end_word": 1}, {"start_word": 2, "end_word": 3}]

    text = "가나다라마바사아 아자차카타파하자 차카타파하가나다라 타파하가나다라마바사."
    result = _split_whisper_segment({
        "start": 1.0,
        "end": 5.0,
        "text": text,
        "words": [
            {"start": 1.1, "end": 1.8, "word": "가나다라마바사아"},
            {"start": 1.9, "end": 2.7, "word": "아자차카타파하자"},
            {"start": 2.8, "end": 3.7, "word": "차카타파하가나다라"},
            {"start": 3.8, "end": 4.8, "word": "타파하가나다라마바사."},
        ],
    }, Analysis())

    assert result[0]["start"] == 1.0
    assert result[0]["end"] == result[1]["start"] == 2.75
    assert result[1]["end"] == 5.0
    assert result[0]["text"] == "가나다라마바사아 아자차카타파하자"
    assert result[1]["text"] == "차카타파하가나다라 타파하가나다라마바사."


def test_long_whisper_segment_prioritizes_silent_gap_without_midpoint():
    class Analysis:
        def split_subtitle_words(self, *_args, **_kwargs):
            raise AssertionError("무음 경계로 만든 짧은 조각은 LLM에 보내면 안 됩니다.")

    result = _split_whisper_segment({
        "start": 1.0,
        "end": 6.0,
        "text": "가나다라마바사 아자차카타파하 차카타파하가나다 타파하가나다라마",
        "words": [
            {"start": 1.1, "end": 1.8, "word": "가나다라마바사"},
            {"start": 1.9, "end": 2.7, "word": "아자차카타파하"},
            {"start": 3.3, "end": 4.1, "word": "차카타파하가나다"},
            {"start": 4.2, "end": 5.8, "word": "타파하가나다라마"},
        ],
    }, Analysis())

    assert WHISPER_LONG_SEGMENT_CHARACTERS == 25
    assert WHISPER_PRIORITY_GAP_SECONDS == 0.5
    assert result[0]["end"] == 2.7
    assert result[1]["start"] == 3.3


def test_whisper_sentence_splits_run_in_parallel_and_preserve_order(monkeypatch):
    active = 0
    maximum = 0
    lock = threading.Lock()

    def fake_split(segment, _service, **_kwargs):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.02)
        with lock:
            active -= 1
        return [{"text": segment["text"]}]

    monkeypatch.setattr(live_edit_pipeline, "_split_whisper_segment", fake_split)
    service = type("Analysis", (), {"_max_parallel_requests": 3})()
    result = _split_whisper_segments_parallel(
        [{"text": str(index)} for index in range(6)],
        service,
    )

    assert maximum > 1
    assert [item["text"] for item in result] == [str(index) for index in range(6)]


def test_timestamp_comment_score_is_applied_to_every_matching_section_by_maximum():
    sections = [
        {"start": 0.0, "end": 60.0},
        {"start": 60.0, "end": 120.0},
        {"start": 120.0, "end": 180.0},
        {"start": 180.0, "end": 240.0},
    ]
    comments = [
        {"text": "0:10 재미있음 1:30 다시 보기"},
        {"text": "01:30 핵심 장면"},
        {"text": "2:59 마지막"},
    ]

    _apply_timestamp_comment_scores(
        sections,
        comments,
        [{"index": 0, "score": 0.7}, {"index": 1, "score": 0.85}, {"index": 2, "score": 0.5}],
    )

    assert _comment_timestamp_seconds(comments[0]["text"]) == [10.0, 90.0]
    assert [section.get("comment_score") for section in sections] == [0.7, 0.85, 0.5, None]


def test_heatmap_score_uses_interpolated_maximum_inside_section():
    heatmap = [
        {"start_time": 0.0, "end_time": 10.0, "value": 0.0},
        {"start_time": 10.0, "end_time": 20.0, "value": 1.0},
        {"start_time": 20.0, "end_time": 30.0, "value": 0.4},
    ]
    sections = [{"start": 7.0, "end": 12.0}, {"start": 16.0, "end": 22.0}]

    _apply_heatmap_scores(sections, heatmap)

    assert _heatmap_section_score(heatmap, 7.0, 12.0) == 0.7
    assert sections[0]["heatmap_score"] == 0.7
    assert sections[1]["heatmap_score"] == 0.94


def test_unsupported_heatmap_does_not_create_score_field():
    sections = [{"start": 0.0, "end": 60.0}]

    _apply_heatmap_scores(sections, [])

    assert "heatmap_score" not in sections[0]


def test_sparse_chat_scores_collective_burst_higher_than_quiet_section():
    chat = [
        {"elapsed_seconds": float(second), "author_id": f"user-{index % 4}"}
        for index, second in enumerate(range(5, 120, 10))
    ]
    chat.extend(
        {"elapsed_seconds": float(second), "author_id": f"burst-{index}"}
        for index, second in enumerate(range(125, 150, 2))
    )
    points = _chat_score_points(chat, 240)
    sections = [{"start": 30.0, "end": 90.0}, {"start": 120.0, "end": 180.0}]

    _apply_point_scores(sections, points, "chat_score")

    assert sections[1]["chat_score"] > sections[0]["chat_score"]


def test_chat_score_is_unsupported_when_fewer_than_ten_events():
    points = _chat_score_points(
        [{"elapsed_seconds": float(index), "author_id": f"user-{index}"} for index in range(9)],
        60,
    )
    assert points == []


def test_chat_score_uses_thirty_second_warmup():
    chat = [
        {"elapsed_seconds": float(index * 2), "author_id": f"user-{index}"}
        for index in range(15)
    ]
    points = _chat_score_points(chat, 120)

    assert all(score == 0.0 for timestamp, score in points if timestamp < 30.0)


def test_chat_analysis_timestamp_compensates_reaction_delay_without_negative_time():
    assert CHAT_REACTION_OFFSET_SECONDS == 3.0
    assert _adjust_chat_timestamp(10.0) == 7.0
    assert _adjust_chat_timestamp(2.0) == 0.0


def test_volume_score_detects_sustained_relative_rise():
    samples = [(float(index), -30.0 if 40 <= index < 45 else -50.0) for index in range(90)]
    points = _volume_score_points(samples)
    quiet = max(score for timestamp, score in points if 10 <= timestamp <= 20)
    loud = max(score for timestamp, score in points if 40 <= timestamp <= 45)

    assert loud > quiet


def test_volume_score_uses_first_thirty_seconds_only_as_warmup():
    samples = [(float(index), -20.0 if 5 <= index < 10 else -50.0) for index in range(60)]
    points = _volume_score_points(samples)

    assert all(score == 0.0 for timestamp, score in points if timestamp < 30.0)


def test_final_score_is_weighted_and_renormalizes_missing_features():
    assert (
        SECTION_SCORE_WEIGHTS["heatmap_score"]
        > SECTION_SCORE_WEIGHTS["comment_score"]
        > SECTION_SCORE_WEIGHTS["llm_score"]
        > SECTION_SCORE_WEIGHTS["chapter_llm_score"]
        > SECTION_SCORE_WEIGHTS["chat_score"]
        > SECTION_SCORE_WEIGHTS["volume_score"]
    )
    assert sum(SECTION_SCORE_WEIGHTS.values()) == pytest.approx(1.0)
    sections = [{
        "chapter_llm_score": 0.8,
        "llm_score": 0.6,
        "heatmap_score": 1.0,
    }]

    _apply_final_scores(sections)

    expected = (
        0.8 * SECTION_SCORE_WEIGHTS["chapter_llm_score"]
        + 0.6 * SECTION_SCORE_WEIGHTS["llm_score"]
        + 1.0 * SECTION_SCORE_WEIGHTS["heatmap_score"]
    ) / (
        SECTION_SCORE_WEIGHTS["chapter_llm_score"]
        + SECTION_SCORE_WEIGHTS["llm_score"]
        + SECTION_SCORE_WEIGHTS["heatmap_score"]
    )
    assert sections[0]["final_score"] == pytest.approx(expected, abs=1e-6)


def test_summary_selection_uses_final_score_instead_of_section_llm_score():
    sections = [
        {"segment_id": "llm-high", "start": 0.0, "end": 60.0, "llm_score": 1.0, "final_score": 0.2},
        {"segment_id": "total-high", "start": 60.0, "end": 120.0, "llm_score": 0.1, "final_score": 0.9},
    ]

    selected = _select_clips(sections, 60)

    assert [item["segment_id"] for item in selected] == ["total-high"]


def test_coherent_selection_preserves_anchor_and_expands_only_required_links():
    class Analysis:
        def __init__(self):
            self.calls = []

        def required_anchor_links(self, anchor_id, _summary, _sections, **_kwargs):
            self.calls.append(anchor_id)
            return ["s0", "s2"] if anchor_id == "s1" else []

    sections = [
        {"segment_id": f"s{index}", "chapter_id": "c0", "chapter_summary": "요약", "start": index * 20.0, "end": (index + 1) * 20.0, "text": str(index), "final_score": score}
        for index, score in enumerate([0.2, 1.0, 0.3, 0.8])
    ]
    analysis = Analysis()

    selected, reviews = _select_coherent_clips(sections, 60, analysis)

    assert [item["segment_id"] for item in selected] == ["s0", "s1", "s2"]
    assert analysis.calls == ["s1"]
    assert reviews == []


def test_coherent_selection_calls_each_chapter_only_once_and_moves_to_next_ranked_chapter():
    class Analysis:
        def __init__(self):
            self.calls = []

        def required_anchor_links(self, anchor_id, _summary, _sections, **_kwargs):
            self.calls.append(anchor_id)
            return []

    sections = [
        {"segment_id": "a-high", "chapter_id": "a", "start": 0.0, "end": 20.0, "text": "", "final_score": 1.0},
        {"segment_id": "a-next", "chapter_id": "a", "start": 20.0, "end": 40.0, "text": "", "final_score": 0.9},
        {"segment_id": "b-high", "chapter_id": "b", "start": 40.0, "end": 60.0, "text": "", "final_score": 0.8},
    ]
    analysis = Analysis()

    selected, _reviews = _select_coherent_clips(sections, 60, analysis)

    assert [item["segment_id"] for item in selected] == ["a-high", "b-high"]
    assert analysis.calls == ["a-high", "b-high"]


def test_atomic_json_write_retries_transient_windows_access_denial(tmp_path, monkeypatch):
    destination = tmp_path / "checkpoint.json"
    real_replace = live_edit_pipeline.os.replace
    attempts = 0

    def transient_failure(source, target):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PermissionError(5, "Access is denied")
        real_replace(source, target)

    monkeypatch.setattr(live_edit_pipeline.os, "replace", transient_failure)
    monkeypatch.setattr(live_edit_pipeline.time, "sleep", lambda _seconds: None)

    _write_json_atomic(destination, {"step": 1})

    assert json.loads(destination.read_text(encoding="utf-8")) == {"step": 1}
    assert attempts == 2
    assert not list(tmp_path.glob(".*.tmp"))


def test_hardware_encoder_detection_uses_supported_gpu_encoder(monkeypatch):
    monkeypatch.setenv("AVE_VIDEO_ENCODER", "auto")
    live_edit_pipeline._render_encoder_candidates.cache_clear()
    monkeypatch.setattr(live_edit_pipeline, "_ffmpeg_binary", lambda: "ffmpeg")

    class Completed:
        returncode = 0
        stdout = " V....D h264_qsv Intel QSV H.264 encoder\n V....D h264_amf AMD AMF H.264 encoder\n V....D h264_nvenc NVIDIA NVENC H.264 encoder\n"
        stderr = ""

    monkeypatch.setattr(live_edit_pipeline.subprocess, "run", lambda *_args, **_kwargs: Completed())
    assert live_edit_pipeline._render_encoder_candidates() == ("h264_nvenc", "h264_amf", "h264_qsv", None)
    assert _video_encoding_args("h264_nvenc")[:2] == ["-c:v", "h264_nvenc"]
    assert _hardware_decoding_args("h264_nvenc") == ["-hwaccel", "auto"]
    assert _hardware_decoding_args(None) == []
    live_edit_pipeline._render_encoder_candidates.cache_clear()


def test_hardware_encoder_priority_prefers_amd_over_intel(monkeypatch):
    monkeypatch.setenv("AVE_VIDEO_ENCODER", "auto")
    live_edit_pipeline._render_encoder_candidates.cache_clear()
    monkeypatch.setattr(live_edit_pipeline, "_ffmpeg_binary", lambda: "ffmpeg")

    class Completed:
        returncode = 0
        stdout = " V....D h264_qsv Intel QSV H.264 encoder\n V....D h264_amf AMD AMF H.264 encoder\n"
        stderr = ""

    monkeypatch.setattr(live_edit_pipeline.subprocess, "run", lambda *_args, **_kwargs: Completed())
    assert live_edit_pipeline._render_encoder_candidates() == ("h264_amf", "h264_qsv", None)
    live_edit_pipeline._render_encoder_candidates.cache_clear()


def test_auto_rendering_tries_remaining_gpus_before_cpu(monkeypatch, tmp_path):
    attempts = []
    statuses = []
    monkeypatch.setattr(live_edit_pipeline, "_render_encoder_candidates", lambda: ("h264_nvenc", "h264_amf", None))

    def render(*args):
        attempts.append(args[-1])
        if args[-1] == "h264_nvenc":
            raise LiveEditPipelineError("GPU를 초기화하지 못했습니다.")
        args[2].write_bytes(b"rendered")

    monkeypatch.setattr(live_edit_pipeline, "_render_final", render)
    backend = live_edit_pipeline.render_final(tmp_path / "source.mp4", [], tmp_path / "output.mp4", status_callback=statuses.append)

    assert attempts == ["h264_nvenc", "h264_amf"]
    assert backend == "AMD GPU (AMF)"
    assert "NVIDIA GPU (NVENC)" in statuses[0]
    assert "AMD GPU (AMF) 렌더링으로 다시 시도" in statuses[1]


def test_auto_rendering_falls_back_to_cpu_after_all_gpus_fail(monkeypatch, tmp_path):
    attempts = []
    monkeypatch.setattr(live_edit_pipeline, "_render_encoder_candidates", lambda: ("h264_nvenc", "h264_amf", None))

    def render(*args):
        attempts.append(args[-1])
        if args[-1] is not None:
            raise LiveEditPipelineError("GPU를 초기화하지 못했습니다.")
        args[2].write_bytes(b"rendered")

    monkeypatch.setattr(live_edit_pipeline, "_render_final", render)
    backend = live_edit_pipeline.render_final(tmp_path / "source.mp4", [], tmp_path / "output.mp4")

    assert attempts == ["h264_nvenc", "h264_amf", None]
    assert backend == "CPU (libx264)"


def test_prepared_transcript_uses_the_requested_language(tmp_path, monkeypatch):
    metadata_dir = tmp_path / "yt-edit" / "dQw4w9WgXcQ"
    metadata_dir.mkdir(parents=True)
    (metadata_dir / "dQw4w9WgXcQ.en.captions-transcript.json").write_text(
        json.dumps({"segments": [{"text": "English"}]}), encoding="utf-8"
    )
    (metadata_dir / "dQw4w9WgXcQ.ko.captions-transcript.json").write_text(
        json.dumps({"segments": [{"text": "한국어"}]}), encoding="utf-8"
    )
    monkeypatch.setattr("app.services.live_youtube_service.get_media_root", lambda: tmp_path)

    assert load_prepared_transcript("dQw4w9WgXcQ", "captions", "ko") == [{"text": "한국어"}]
    with pytest.raises(LiveYouTubeError, match="en subtitles"):
        load_prepared_transcript("dQw4w9WgXcQ", "subtitles", "en")


def test_selection_render_preserves_render_intermediates(tmp_path, monkeypatch):
    job_id = "review-job"
    output_dir = tmp_path / "yt-edit" / job_id
    output_dir.mkdir(parents=True)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    plan = {
        "source_video_path": str(source),
        "candidates": [{"segment_id": "segment-a", "start": 10.0, "end": 20.0, "text": "후보"}],
        "recommended_segment_ids": ["segment-a"],
        "selected_segment_ids": ["segment-a"],
        "clips": [{"start": 9.6, "end": 20.6}],
    }
    plan["script_segments"] = [{"start": 10.0, "end": 20.0, "text": "후보"}]

    def fake_render(_source, _clips, output, subtitles, *_args, **_kwargs):
        assert subtitles.is_file()
        output.write_bytes(b"rendered")

    monkeypatch.setattr("app.services.live_edit_pipeline.render_final", fake_render)
    result = LiveEditPipeline(tmp_path).rerender_from_selection(job_id, ["segment-a"], plan=plan)

    assert (output_dir / result["rendered_filename"]).read_bytes() == b"rendered"
    assert list(output_dir.glob(f"{job_id}.render-input.*.srt"))
    assert list(output_dir.glob(f"{job_id}.edited.*.pending.mp4"))


def test_selection_rejects_unknown_segment(tmp_path):
    try:
        LiveEditPipeline(tmp_path).rerender_from_selection(
            "bad-selection", ["missing"],
            plan={"candidates": [{"segment_id": "chapter-00", "start": 0, "end": 10}]},
        )
    except LiveEditPipelineError as exc:
        assert "존재하지 않는" in str(exc)
    else:
        raise AssertionError("unknown segment must be rejected")


def test_review_exposes_chapter_section_hierarchy(tmp_path):
    review = LiveEditPipeline(tmp_path).get_segment_review("review-job", {
        "target_seconds": 60,
        "selected_segment_ids": ["chapter-00-section-00"],
        "recommended_segment_ids": ["chapter-00-section-00"],
        "candidates": [{"segment_id": "chapter-00-section-00", "chapter_id": "chapter-00", "section_id": "chapter-00-section-00", "start": 0, "end": 4, "text": "섹션", "llm_score": 0.9}],
        "chapters": [{"chapter_id": "chapter-00", "summary": "주제 요약", "llm_score": 0.812, "start": 0, "end": 4, "sections": [{"section_id": "chapter-00-section-00", "start": 0, "end": 4, "segment_ids": ["chapter-00-section-00"]}]}],
    })

    assert review["chapters"][0]["sections"][0]["segment_ids"] == ["chapter-00-section-00"]
    assert "segments" not in review
    assert review["chapters"][0]["llm_score"] == 0.812
    assert review["chapters"][0]["sections"][0]["llm_score"] == 0.9
    assert "final_score" not in review["chapters"][0]["sections"][0]
    assert "volume_score" not in review["chapters"][0]["sections"][0]
    assert "chat_score" not in review["chapters"][0]["sections"][0]

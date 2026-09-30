import json
import threading
import time
from types import SimpleNamespace

import pytest

from app.services import live_edit_pipeline
from app.services.live_edit_pipeline import (
    CHAT_REACTION_OFFSET_SECONDS,
    SECTION_SCORE_WEIGHTS,
    LiveEditPipeline,
    LiveEditPipelineError,
    SUBTITLE_FONT_SIZE,
    SUBTITLE_LINE_WIDTH,
    SUBTITLE_MARGIN_BOTTOM,
    SUBTITLE_MIN_DURATION_SECONDS,
    WHISPER_SPLIT_MIN_CHARACTERS,
    WHISPER_PRIORITY_GAP_SECONDS,
    _adjust_chat_timestamp,
    _apply_final_scores,
    _apply_heatmap_scores,
    _apply_point_scores,
    _apply_timestamp_comment_scores,
    _chat_score_points,
    _comment_timestamp_seconds,
    _hardware_decoding_args,
    _heatmap_section_score,
    _select_clips,
    _select_coherent_clips,
    _shared_whisper_source_path,
    _shared_whisper_transcript_path,
    _split_whisper_segment,
    _split_whisper_segments_parallel,
    _time_seconds,
    _video_encoding_args,
    _volume_score_points,
    _whisper_transcript_path,
    _write_json_atomic,
    fixed_whisper_initial_prompt,
    has_default_whisper_transcript_cache,
    write_selected_subtitles,
)
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
            {
                "start": 1.1,
                "end": 2.0,
                "text": "하나 둘 셋 넷 다섯 여섯 일곱 여덟 아홉 열 하나 둘 셋 넷 다섯 여섯",
            },
        ],
        [{"start": 0.0, "end": 2.0}],
        output,
    )

    assert (SUBTITLE_FONT_SIZE, SUBTITLE_MARGIN_BOTTOM, SUBTITLE_LINE_WIDTH) == (
        16,
        12,
        30,
    )
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


def test_shared_whisper_source_path_is_stable_for_the_same_settings(tmp_path):
    request = {
        "video_id": "dQw4w9WgXcQ",
        "audio_size": 123,
        "audio_mtime_ns": 456,
        "language": "ko",
        "initial_prompt": "",
        "hotwords": "OpenAI",
        "speed": 1.0,
        "engine": "whisperx-aligned-word-v1",
        "alignment": "ctc-forced-alignment-with-words",
    }

    first = _shared_whisper_source_path(tmp_path, "dQw4w9WgXcQ", request)
    second = _shared_whisper_source_path(
        tmp_path, "dQw4w9WgXcQ", dict(reversed(list(request.items())))
    )
    changed = _shared_whisper_source_path(
        tmp_path, "dQw4w9WgXcQ", {**request, "speed": 2.0}
    )

    assert first == second
    assert first != changed
    assert first.parent == tmp_path / "yt-edit" / "dQw4w9WgXcQ" / "whisper-cache"


def test_default_whisper_cache_is_detected_only_for_default_settings(tmp_path):
    video_id = "dQw4w9WgXcQ"
    request = {
        "video_id": video_id,
        "audio_size": 123,
        "audio_mtime_ns": 456,
        "language": "ko",
        "initial_prompt": "",
        "hotwords": "",
        "speed": 1.0,
        "engine": "whisperx-aligned-word-v1",
        "alignment": "ctc-forced-alignment-with-words",
    }
    path = _shared_whisper_source_path(tmp_path, video_id, request)
    _write_json_atomic(
        path,
        {
            "request": request,
            "engine": request["engine"],
            "alignment": request["alignment"],
            "segments": [{"start": 0, "end": 1, "text": "캐시"}],
        },
    )

    assert has_default_whisper_transcript_cache(tmp_path, video_id) is True
    request["speed"] = 1.5
    _write_json_atomic(
        path,
        {
            "request": request,
            "engine": "whisperx-aligned-word-v1",
            "alignment": "ctc-forced-alignment-with-words",
            "segments": [{"start": 0, "end": 1, "text": "캐시"}],
        },
    )
    assert has_default_whisper_transcript_cache(tmp_path, video_id) is False


def test_whisper_reuses_matching_video_level_source_cache(tmp_path, monkeypatch):
    video_id = "dQw4w9WgXcQ"
    audio = tmp_path / "source.mp3"
    audio.write_bytes(b"same audio")
    audio_stat = audio.stat()
    request = {
        "video_id": video_id,
        "audio_size": audio_stat.st_size,
        "audio_mtime_ns": audio_stat.st_mtime_ns,
        "language": "ko",
        "initial_prompt": "",
        "hotwords": "",
        "speed": 1.0,
        "engine": "whisperx-aligned-word-v1",
        "alignment": "ctc-forced-alignment-with-words",
    }
    shared = _shared_whisper_source_path(tmp_path, video_id, request)
    _write_json_atomic(
        shared,
        {
            "request": request,
            "language": "ko",
            "engine": request["engine"],
            "alignment": request["alignment"],
            "segments": [{"start": 0.0, "end": 1.0, "text": "재사용 문장"}],
        },
    )
    monkeypatch.setattr(
        live_edit_pipeline.YouTubeImporter,
        "prepare_best_audio",
        lambda *_args, **_kwargs: audio,
    )
    monkeypatch.setattr(
        live_edit_pipeline,
        "upload_audio_for_transcription",
        lambda *_args, **_kwargs: pytest.fail("공유 캐시가 있으면 업로드하면 안 됩니다."),
    )

    result = LiveEditPipeline(tmp_path).prepare_whisper_transcript(
        job_id="new-job",
        vod_url=f"https://www.youtube.com/watch?v={video_id}",
        llm_provider="deepseek",
        stt_language="ko",
        stt_initial_prompt="",
        stt_hotwords="",
        stt_speed=1.0,
        server_access_token="Bearer session",
    )

    assert result["segment_count"] == 1
    job_source = tmp_path / "yt-edit" / "new-job" / "new-job.whisper-source.json"
    assert json.loads(job_source.read_text(encoding="utf-8"))["segments"][0][
        "text"
    ] == "재사용 문장"


def test_whisper_reports_local_steps_after_backend_completion(tmp_path, monkeypatch):
    audio = tmp_path / "source.mp3"
    audio.write_bytes(b"audio")
    monkeypatch.setattr(
        live_edit_pipeline.YouTubeImporter,
        "prepare_best_audio",
        lambda *_args, **kwargs: (
            kwargs["progress_callback"](99), audio
        )[1],
    )
    monkeypatch.setattr(
        live_edit_pipeline, "prepare_whisper_audio", lambda *_args: audio
    )
    monkeypatch.setattr(
        live_edit_pipeline, "upload_audio_for_transcription",
        lambda *_args: SimpleNamespace(file_id="uploaded"),
    )

    def transcribe(*_args, **kwargs):
        kwargs["progress_callback"](100, "Whisper 전사가 완료되었습니다.")
        return {
            "engine": live_edit_pipeline.WHISPER_ENGINE,
            "alignment": live_edit_pipeline.WHISPER_ALIGNMENT,
            "segments": [{"start": 0.0, "end": 1.0, "text": "짧은 문장"}],
        }

    monkeypatch.setattr(live_edit_pipeline, "transcribe_uploaded_audio", transcribe)
    progress = []
    backend = []
    result = LiveEditPipeline(tmp_path).prepare_whisper_transcript(
        job_id="progress-job",
        vod_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        llm_provider="deepseek",
        stt_language="ko",
        stt_initial_prompt="",
        stt_hotwords="",
        stt_speed=1.0,
        server_access_token="Bearer session",
        progress_callback=lambda value, message: progress.append((value, message)),
        backend_progress_callback=backend.append,
    )
    assert result["segment_count"] == 1
    assert backend == [100]
    assert any(value == 80 and "문장 분할" in message for value, message in progress)
    assert progress[-1][0] == 100


def test_whisper_reuses_video_level_sentence_split_cache(tmp_path, monkeypatch):
    video_id = "dQw4w9WgXcQ"
    audio = tmp_path / "source.mp3"
    audio.write_bytes(b"same audio")
    audio_stat = audio.stat()
    request = {
        "video_id": video_id,
        "audio_size": audio_stat.st_size,
        "audio_mtime_ns": audio_stat.st_mtime_ns,
        "language": "ko",
        "initial_prompt": "",
        "hotwords": "",
        "speed": 1.0,
        "engine": "whisperx-aligned-word-v1",
        "alignment": "ctc-forced-alignment-with-words",
    }
    source_path = _shared_whisper_source_path(tmp_path, video_id, request)
    long_segment = {
        "start": 0.0,
        "end": 2.0,
        "text": "가" * 30,
        "words": [
            {"word": "가" * 15, "start": 0.0, "end": 0.9},
            {"word": "가" * 15, "start": 1.0, "end": 2.0},
        ],
    }
    _write_json_atomic(
        source_path,
        {
            "request": request,
            "engine": request["engine"],
            "alignment": request["alignment"],
            "segments": [long_segment],
        },
    )
    split_calls = 0

    def split_once(*_args, **_kwargs):
        nonlocal split_calls
        split_calls += 1
        return [{"start": 0.0, "end": 2.0, "text": "분할 결과"}]

    monkeypatch.setattr(
        live_edit_pipeline.YouTubeImporter,
        "prepare_best_audio",
        lambda *_args, **_kwargs: audio,
    )
    monkeypatch.setattr(
        live_edit_pipeline, "_split_whisper_segments_parallel", split_once
    )

    pipeline = LiveEditPipeline(tmp_path)
    arguments = {
        "vod_url": f"https://www.youtube.com/watch?v={video_id}",
        "llm_provider": "deepseek",
        "stt_language": "ko",
        "stt_initial_prompt": "",
        "stt_hotwords": "",
        "stt_speed": 1.0,
        "server_access_token": "Bearer session",
    }
    pipeline.prepare_whisper_transcript(job_id="first-job", **arguments)
    pipeline.prepare_whisper_transcript(job_id="second-job", **arguments)

    assert split_calls == 1
    shared_transcript, _ = _shared_whisper_transcript_path(
        tmp_path, video_id, request, "deepseek"
    )
    assert shared_transcript.is_file()
    second = json.loads(
        (tmp_path / "yt-edit" / "second-job" / "second-job.whisper-transcript.json").read_text(
            encoding="utf-8"
        )
    )
    assert second["segments"] == [{"start": 0.0, "end": 2.0, "text": "분할 결과"}]


def test_long_whisper_segment_uses_midpoint_between_adjacent_words():
    class Analysis:
        def split_subtitle_words(self, _words, **_kwargs):
            return [{"start_word": 0, "end_word": 1}, {"start_word": 2, "end_word": 3}]

    text = "가나다라마바사아 아자차카타파하자 차카타파하가나다라 타파하가나다라마바사."
    result = _split_whisper_segment(
        {
            "start": 1.0,
            "end": 5.0,
            "text": text,
            "words": [
                {"start": 1.1, "end": 1.8, "word": "가나다라마바사아"},
                {"start": 1.9, "end": 2.7, "word": "아자차카타파하자"},
                {"start": 2.8, "end": 3.7, "word": "차카타파하가나다라"},
                {"start": 3.8, "end": 4.8, "word": "타파하가나다라마바사."},
            ],
        },
        Analysis(),
    )

    assert result[0]["start"] == 1.0
    assert result[0]["end"] == result[1]["start"] == 2.75
    assert result[1]["end"] == 5.0
    assert result[0]["text"] == "가나다라마바사아 아자차카타파하자"
    assert result[1]["text"] == "차카타파하가나다라 타파하가나다라마바사."


def test_whisper_segment_with_exactly_thirty_characters_uses_llm_split():
    class Analysis:
        called = False

        def split_subtitle_words(self, _words, **_kwargs):
            self.called = True
            return [{"start_word": 0, "end_word": 0}, {"start_word": 1, "end_word": 1}]

    analysis = Analysis()
    result = _split_whisper_segment(
        {
            "start": 1.0,
            "end": 3.0,
            "text": f'{"가" * 15} {"나" * 15}',
            "words": [
                {"start": 1.0, "end": 1.9, "word": "가" * 15},
                {"start": 2.0, "end": 3.0, "word": "나" * 15},
            ],
        },
        analysis,
    )

    assert analysis.called is True
    assert len(result) == 2


def test_long_whisper_segment_prioritizes_silent_gap_without_midpoint():
    class Analysis:
        def split_subtitle_words(self, *_args, **_kwargs):
            raise AssertionError("무음 경계로 만든 짧은 조각은 LLM에 보내면 안 됩니다.")

    result = _split_whisper_segment(
        {
            "start": 1.0,
            "end": 6.0,
            "text": "가나다라마바사 아자차카타파하 차카타파하가나다 타파하가나다라마",
            "words": [
                {"start": 1.1, "end": 1.8, "word": "가나다라마바사"},
                {"start": 1.9, "end": 2.7, "word": "아자차카타파하"},
                {"start": 3.3, "end": 4.1, "word": "차카타파하가나다"},
                {"start": 4.2, "end": 5.8, "word": "타파하가나다라마"},
            ],
        },
        Analysis(),
    )

    assert WHISPER_SPLIT_MIN_CHARACTERS == 30
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
        [
            {"index": 0, "score": 0.7},
            {"index": 1, "score": 0.85},
            {"index": 2, "score": 0.5},
        ],
    )

    assert _comment_timestamp_seconds(comments[0]["text"]) == [10.0, 90.0]
    assert [section.get("comment_score") for section in sections] == [
        0.7,
        0.85,
        0.5,
        None,
    ]


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
        [
            {"elapsed_seconds": float(index), "author_id": f"user-{index}"}
            for index in range(9)
        ],
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
    samples = [
        (float(index), -30.0 if 40 <= index < 45 else -50.0) for index in range(90)
    ]
    points = _volume_score_points(samples)
    quiet = max(score for timestamp, score in points if 10 <= timestamp <= 20)
    loud = max(score for timestamp, score in points if 40 <= timestamp <= 45)

    assert loud > quiet


def test_volume_score_uses_first_thirty_seconds_only_as_warmup():
    samples = [
        (float(index), -20.0 if 5 <= index < 10 else -50.0) for index in range(60)
    ]
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
    sections = [
        {
            "chapter_llm_score": 0.8,
            "llm_score": 0.6,
            "heatmap_score": 1.0,
        }
    ]

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
        {
            "segment_id": "llm-high",
            "start": 0.0,
            "end": 60.0,
            "llm_score": 1.0,
            "final_score": 0.2,
        },
        {
            "segment_id": "total-high",
            "start": 60.0,
            "end": 120.0,
            "llm_score": 0.1,
            "final_score": 0.9,
        },
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
        {
            "segment_id": f"s{index}",
            "chapter_id": "c0",
            "chapter_summary": "요약",
            "start": index * 20.0,
            "end": (index + 1) * 20.0,
            "text": str(index),
            "final_score": score,
        }
        for index, score in enumerate([0.2, 1.0, 0.3, 0.8])
    ]
    analysis = Analysis()

    selected, reviews = _select_coherent_clips(sections, 59, analysis)

    assert [item["segment_id"] for item in selected] == ["s0", "s1", "s2"]
    assert analysis.calls == ["s1"]
    assert reviews == []


@pytest.mark.parametrize("duration", [0.05, 1.0, 3.0, 4.999, 5.0])
def test_coherent_selection_accepts_short_sentence_anchors(duration):
    class Analysis:
        def required_anchor_links(self, anchor_id, _summary, sections, **_kwargs):
            assert anchor_id == "short"
            assert [row["id"] for row in sections] == ["short"]
            return []

    sections = [{"segment_id": "short", "chapter_id": "c", "start": 0.0,
                 "end": duration, "final_score": 1.0, "text": "핵심 문장."}]
    selected, _ = _select_coherent_clips(sections, 10, Analysis())
    assert selected == sections


def test_short_anchor_and_short_required_context_are_both_selectable():
    class Analysis:
        def __init__(self):
            self.calls = []

        def required_anchor_links(self, anchor_id, _summary, sections, **_kwargs):
            self.calls.append(anchor_id)
            assert [row["id"] for row in sections] == ["context", "short-high", "long-low"]
            return ["context"]

    sections = [
        {"segment_id": "context", "chapter_id": "c", "start": 0.0,
         "end": 0.25, "final_score": 0.1},
        {"segment_id": "short-high", "chapter_id": "c", "start": 0.25,
         "end": 3.25, "final_score": 1.0},
        {"segment_id": "long-low", "chapter_id": "c", "start": 3.25,
         "end": 13.25, "final_score": 0.5},
    ]
    analysis = Analysis()
    selected, _ = _select_coherent_clips(sections, 2, analysis)
    assert [row["segment_id"] for row in selected] == ["context", "short-high"]
    assert analysis.calls == ["short-high"]


@pytest.mark.parametrize("duration", [0.0, -1.0, float("inf"), float("nan")])
def test_selection_still_excludes_invalid_durations(duration):
    class Analysis:
        def required_anchor_links(self, *_args, **_kwargs):
            pytest.fail("유효하지 않은 구간을 LLM에 보내면 안 됩니다.")

    sections = [{"segment_id": "invalid", "start": 0.0, "end": duration}]
    assert _select_coherent_clips(sections, 10, Analysis()) == ([], [])
    assert _select_clips(sections, 10) == []


def test_score_only_selection_also_accepts_sub_five_second_sections():
    sections = [
        {"segment_id": "short", "start": 0.0, "end": 3.0, "final_score": 1.0},
        {"segment_id": "long", "start": 3.0, "end": 13.0, "final_score": 0.1},
    ]
    assert _select_clips(sections, 3) == [sections[0]]


def test_coherent_selection_checks_ranked_anchors_even_in_the_same_chapter():
    class Analysis:
        def __init__(self):
            self.calls = []

        def required_anchor_links(self, anchor_id, _summary, _sections, **_kwargs):
            self.calls.append(anchor_id)
            return []

    sections = [
        {
            "segment_id": "a-high",
            "chapter_id": "a",
            "start": 0.0,
            "end": 20.0,
            "text": "",
            "final_score": 1.0,
        },
        {
            "segment_id": "a-next",
            "chapter_id": "a",
            "start": 20.0,
            "end": 40.0,
            "text": "",
            "final_score": 0.9,
        },
        {
            "segment_id": "b-high",
            "chapter_id": "b",
            "start": 40.0,
            "end": 60.0,
            "text": "",
            "final_score": 0.8,
        },
    ]
    analysis = Analysis()

    selected, _reviews = _select_coherent_clips(sections, 59, analysis)

    assert [item["segment_id"] for item in selected] == [
        "a-high",
        "a-next",
        "b-high",
    ]
    assert analysis.calls == ["a-high", "a-next", "b-high"]


def test_anchor_link_parallelism_tracks_quarter_of_remaining_sections(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor as RealExecutor

    worker_counts = []

    class RecordingExecutor(RealExecutor):
        def __init__(self, max_workers, *args, **kwargs):
            worker_counts.append(max_workers)
            super().__init__(max_workers=max_workers, *args, **kwargs)

    class Analysis:
        _max_parallel_requests = 10

        def required_anchor_links(self, *_args, **_kwargs):
            return []

    monkeypatch.setattr(live_edit_pipeline, "ThreadPoolExecutor", RecordingExecutor)
    sections = [
        {
            "segment_id": f"s{index}",
            "chapter_id": "c0",
            "start": index * 10.0,
            "end": (index + 1) * 10.0,
            "text": str(index),
            "final_score": 1.0 - index / 100,
        }
        for index in range(10)
    ]

    _select_coherent_clips(sections, 1_000, Analysis())

    assert worker_counts == [3, 2, 2, 1, 1, 1]


def test_atomic_json_write_retries_transient_windows_access_denial(
    tmp_path, monkeypatch
):
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

    monkeypatch.setattr(
        live_edit_pipeline.subprocess, "run", lambda *_args, **_kwargs: Completed()
    )
    assert live_edit_pipeline._render_encoder_candidates() == (
        "h264_nvenc",
        "h264_amf",
        "h264_qsv",
        None,
    )
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

    monkeypatch.setattr(
        live_edit_pipeline.subprocess, "run", lambda *_args, **_kwargs: Completed()
    )
    assert live_edit_pipeline._render_encoder_candidates() == (
        "h264_amf",
        "h264_qsv",
        None,
    )
    live_edit_pipeline._render_encoder_candidates.cache_clear()


def test_auto_rendering_tries_remaining_gpus_before_cpu(monkeypatch, tmp_path):
    attempts = []
    statuses = []
    monkeypatch.setattr(
        live_edit_pipeline,
        "_render_encoder_candidates",
        lambda: ("h264_nvenc", "h264_amf", None),
    )

    def render(*args):
        attempts.append(args[-1])
        if args[-1] == "h264_nvenc":
            raise LiveEditPipelineError("GPU를 초기화하지 못했습니다.")
        args[2].write_bytes(b"rendered")

    monkeypatch.setattr(live_edit_pipeline, "_render_final", render)
    backend = live_edit_pipeline.render_final(
        tmp_path / "source.mp4",
        [],
        tmp_path / "output.mp4",
        status_callback=statuses.append,
    )

    assert attempts == ["h264_nvenc", "h264_amf"]
    assert backend == "AMD GPU (AMF)"
    assert "NVIDIA GPU (NVENC)" in statuses[0]
    assert "AMD GPU (AMF) 렌더링으로 다시 시도" in statuses[1]


def test_auto_rendering_falls_back_to_cpu_after_all_gpus_fail(monkeypatch, tmp_path):
    attempts = []
    monkeypatch.setattr(
        live_edit_pipeline,
        "_render_encoder_candidates",
        lambda: ("h264_nvenc", "h264_amf", None),
    )

    def render(*args):
        attempts.append(args[-1])
        if args[-1] is not None:
            raise LiveEditPipelineError("GPU를 초기화하지 못했습니다.")
        args[2].write_bytes(b"rendered")

    monkeypatch.setattr(live_edit_pipeline, "_render_final", render)
    backend = live_edit_pipeline.render_final(
        tmp_path / "source.mp4", [], tmp_path / "output.mp4"
    )

    assert attempts == ["h264_nvenc", "h264_amf", None]
    assert backend == "CPU (libx264)"


def test_render_combines_clips_and_subtitles_in_one_ffmpeg_pass(monkeypatch, tmp_path):
    subtitles = tmp_path / "captions.srt"
    subtitles.write_text("1\n00:00:00,000 --> 00:00:01,000\n자막\n", encoding="utf-8")
    commands = []

    def capture(command, **kwargs):
        commands.append((command, kwargs))

    monkeypatch.setattr(live_edit_pipeline, "_ffmpeg_binary", lambda: "ffmpeg")
    monkeypatch.setattr(live_edit_pipeline, "_run_ffmpeg", capture)

    live_edit_pipeline._render_final(
        tmp_path / "source.mp4",
        [{"start": 10.0, "end": 12.0}],
        tmp_path / "output.mp4",
        subtitles,
        encoder="h264_amf",
    )

    assert len(commands) == 1
    command, options = commands[0]
    filter_graph = command[command.index("-filter_complex") + 1]
    assert "[0:v]select='between(t\\,10.000000\\,12.000000)'" in filter_graph
    assert "setpts=N/FRAME_RATE/TB[selectedv]" in filter_graph
    assert "[0:a]aselect='between(t\\,10.000000\\,12.000000)'" in filter_graph
    assert "asetpts=N/SR/TB[a]" in filter_graph
    assert "[selectedv]subtitles=filename=" in filter_graph
    assert command[command.index("-i") - 2 : command.index("-i")] == [
        "-t",
        "12.000000",
    ]
    assert command[-1] == str(tmp_path / "output.mp4")
    assert options["duration_seconds"] == 2.0


def test_render_balances_large_selection_expression(monkeypatch, tmp_path):
    commands = []
    monkeypatch.setattr(live_edit_pipeline, "_ffmpeg_binary", lambda: "ffmpeg")
    monkeypatch.setattr(
        live_edit_pipeline,
        "_run_ffmpeg",
        lambda command, **_kwargs: commands.append(command),
    )
    clips = [
        {"start": float(index * 10), "end": float(index * 10 + 5)}
        for index in range(256)
    ]

    live_edit_pipeline._render_final(
        tmp_path / "source.mp4", clips, tmp_path / "output.mp4"
    )

    filter_graph = commands[0][commands[0].index("-filter_complex") + 1]
    selection = filter_graph.split("select='", 1)[1].split("',setpts", 1)[0]
    assert selection.count("between(") == 256
    assert selection.count("(") > 256
    assert selection.startswith("((((((((between(")


def test_render_sorts_and_merges_overlapping_selection_intervals(monkeypatch, tmp_path):
    commands = []
    monkeypatch.setattr(live_edit_pipeline, "_ffmpeg_binary", lambda: "ffmpeg")
    monkeypatch.setattr(
        live_edit_pipeline,
        "_run_ffmpeg",
        lambda command, **_kwargs: commands.append(command),
    )

    live_edit_pipeline._render_final(
        tmp_path / "source.mp4",
        [
            {"start": 20.0, "end": 25.0},
            {"start": 10.0, "end": 15.0},
            {"start": 14.0, "end": 21.0},
        ],
        tmp_path / "output.mp4",
    )

    filter_graph = commands[0][commands[0].index("-filter_complex") + 1]
    selection = filter_graph.split("select='", 1)[1].split("',setpts", 1)[0]
    assert selection == "between(t\\,10.000000\\,25.000000)"
    assert commands[0][commands[0].index("-i") - 1] == "25.000000"


def test_prepared_transcript_uses_the_requested_language(tmp_path, monkeypatch):
    metadata_dir = tmp_path / "yt-edit" / "dQw4w9WgXcQ"
    metadata_dir.mkdir(parents=True)
    (metadata_dir / "dQw4w9WgXcQ.en.captions-transcript.json").write_text(
        json.dumps({"segments": [{"text": "English"}]}), encoding="utf-8"
    )
    (metadata_dir / "dQw4w9WgXcQ.ko.captions-transcript.json").write_text(
        json.dumps({"segments": [{"text": "한국어"}]}), encoding="utf-8"
    )
    monkeypatch.setattr(
        "app.services.live_youtube_service.get_media_root", lambda: tmp_path
    )

    assert load_prepared_transcript("dQw4w9WgXcQ", "captions", "ko") == [
        {"text": "한국어"}
    ]
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
        "candidates": [
            {"segment_id": "segment-a", "start": 10.0, "end": 20.0, "text": "후보"}
        ],
        "recommended_segment_ids": ["segment-a"],
        "selected_segment_ids": ["segment-a"],
        "clips": [{"start": 9.6, "end": 20.6}],
    }
    plan["script_segments"] = [{"start": 10.0, "end": 20.0, "text": "후보"}]

    def fake_render(_source, _clips, output, subtitles, *_args, **_kwargs):
        assert subtitles.is_file()
        output.write_bytes(b"rendered")

    monkeypatch.setattr("app.services.live_edit_pipeline.render_final", fake_render)
    result = LiveEditPipeline(tmp_path).rerender_from_selection(
        job_id, ["segment-a"], plan=plan
    )

    assert (output_dir / result["rendered_filename"]).read_bytes() == b"rendered"
    assert list(output_dir.glob(f"{job_id}.render-input.*.srt"))
    assert list(output_dir.glob(f"{job_id}.edited.*.pending.mp4"))


def test_selection_rejects_unknown_segment(tmp_path):
    try:
        LiveEditPipeline(tmp_path).rerender_from_selection(
            "bad-selection",
            ["missing"],
            plan={"candidates": [{"segment_id": "chapter-00", "start": 0, "end": 10}]},
        )
    except LiveEditPipelineError as exc:
        assert "존재하지 않는" in str(exc)
    else:
        raise AssertionError("unknown segment must be rejected")


def test_review_exposes_chapter_section_hierarchy(tmp_path):
    review = LiveEditPipeline(tmp_path).get_segment_review(
        "review-job",
        {
            "target_seconds": 60,
            "selected_segment_ids": ["chapter-00-section-00"],
            "recommended_segment_ids": ["chapter-00-section-00"],
            "candidates": [
                {
                    "segment_id": "chapter-00-section-00",
                    "chapter_id": "chapter-00",
                    "section_id": "chapter-00-section-00",
                    "start": 0,
                    "end": 4,
                    "text": "섹션",
                    "llm_score": 0.9,
                }
            ],
            "chapters": [
                {
                    "chapter_id": "chapter-00",
                    "summary": "주제 요약",
                    "llm_score": 0.812,
                    "start": 0,
                    "end": 4,
                    "sections": [
                        {
                            "section_id": "chapter-00-section-00",
                            "start": 0,
                            "end": 4,
                            "segment_ids": ["chapter-00-section-00"],
                        }
                    ],
                }
            ],
        },
    )

    assert review["chapters"][0]["sections"][0]["segment_ids"] == [
        "chapter-00-section-00"
    ]
    assert "segments" not in review
    assert review["chapters"][0]["llm_score"] == 0.812
    assert review["chapters"][0]["sections"][0]["llm_score"] == 0.9
    assert "final_score" not in review["chapters"][0]["sections"][0]
    assert "volume_score" not in review["chapters"][0]["sections"][0]
    assert "chat_score" not in review["chapters"][0]["sections"][0]

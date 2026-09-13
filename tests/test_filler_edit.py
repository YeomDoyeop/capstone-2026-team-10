import json
import subprocess
from pathlib import Path

import pytest

from app.services.filler_edit import apply_filler_cuts, filler_candidates, timed_units
from app.services.llm_analysis_service import LLMAnalysisError, LLMAnalysisService
from app.services.live_edit_pipeline import LiveEditPipeline, _render_final, _split_whisper_segment, write_selected_subtitles
from app.services.live_youtube_service import _parse_vtt_rows, _rolling_caption_rows


def sample():
    return [{"start": 0.0, "end": 5.0, "text": "네. GPT가 출시됐습니다. 네.", "words": [
        {"word": "네.", "start": 0.1, "end": 0.3},
        {"word": "GPT가", "start": 0.4, "end": 1.3},
        {"word": "출시됐습니다.", "start": 1.4, "end": 4.1},
        {"word": "네.", "start": 4.3, "end": 4.5},
    ]}]


def test_actual_cuts_preserve_news_and_remap_subtitles(tmp_path):
    segments = sample()
    cuts, missing = filler_candidates(segments)
    assert len(cuts) == 2 and missing == 0
    clips, cleaned, stats = apply_filler_cuts(segments, [{"start": 0, "end": 5}], cuts)
    assert sum(c["end"] - c["start"] for c in clips) == pytest.approx(4.6)
    assert cleaned[0]["text"] == "GPT가 출시됐습니다."
    assert segments[0]["text"].startswith("네.")  # 원본과 재선택의 입력은 보존
    assert stats["removed_count"] == 2
    output = tmp_path / "edited.srt"
    write_selected_subtitles(cleaned, clips, output)
    srt = output.read_text(encoding="utf-8")
    assert "네." not in srt and "GPT가 출시됐습니다." in srt
    assert "00:00:04,600" in srt


def test_partial_selection_does_not_remove_partial_word_or_unselected_text():
    segments = sample()
    cuts, _ = filler_candidates(segments)
    clips, cleaned, stats = apply_filler_cuts(segments, [{"start": 0.2, "end": 4.2}], cuts)
    assert stats["removed_count"] == 0
    assert cleaned == segments
    assert clips == [{"start": 0.2, "end": 4.2}]


def test_missing_or_invalid_timestamps_never_guess_internal_cut():
    segments = [{"start": 0, "end": 10, "text": "네. 핵심 뉴스입니다. 네트워크 음성 아마"}]
    assert filler_candidates(segments) == ([], 1)
    segments[0]["words"] = [{"word": "네.", "start": 0, "end": 1}]
    assert timed_units(segments[0]) == []  # 원문 일부만 정렬되어 있으면 보존
    assert filler_candidates(segments)[0] == []
    broken = sample()
    broken[0]["words"][0]["end"] = float("nan")
    assert timed_units(broken[0]) == []


def test_removal_guard_prevents_large_duration_loss():
    segments = [{"start": 0, "end": 1, "text": "네."}]
    cuts, _ = filler_candidates(segments)
    clips = [{"start": 0, "end": 2}]
    edited, cleaned, stats = apply_filler_cuts(segments, clips, cuts)
    assert edited == clips and cleaned == segments and stats["guard_triggered"]


def test_llm_selects_only_candidate_ids_and_keeps_affirmative_answer(monkeypatch):
    agent = object.__new__(LLMAnalysisService)
    candidates = [{"id": 0, "text": "네", "before": "출시됐나요?", "after": "출시됐습니다."},
                  {"id": 1, "text": "네", "before": "", "after": "이번 소식입니다."}]

    def request(system, prompt, **kwargs):
        assert "긍정 답변" in system
        assert "출시됐나요?" in prompt
        return kwargs["validator"]({"remove_ids": [1]})

    monkeypatch.setattr(agent, "_request_json", request)
    assert agent.detect_fillers(candidates) == [candidates[1]]
    for invalid in ([2], [True], [1, 1], "1"):
        monkeypatch.setattr(agent, "_request_json", lambda *a, **kw: kw["validator"]({"remove_ids": invalid}))
        with pytest.raises(LLMAnalysisError):
            agent.detect_fillers(candidates)


def test_whisper_short_segment_retains_word_alignment():
    result = _split_whisper_segment(sample()[0], None)
    assert result[0]["words"] == sample()[0]["words"]
    assert len(filler_candidates(result)[0]) == 2


def test_youtube_inline_times_survive_rolling_completion():
    rows = _parse_vtt_rows(
        "WEBVTT\n\n00:00:00.000 --> 00:00:02.000\n"
        "이전 자막\n네.<00:00:00.200><c> GPT가</c><00:00:00.900><c> 나왔습니다.</c>\n\n"
        "00:00:02.000 --> 00:00:02.010\n네. GPT가 나왔습니다.\n", "sample.vtt")
    restored = _rolling_caption_rows(rows)
    segment = {**restored[0], "start": 0, "end": 2}
    options, _ = filler_candidates([segment])
    assert len(options) == 1
    assert options[0]["start"] == 0 and options[0]["end"] == 0.2


def test_rerender_uses_cut_ranges_and_saves_actual_duration(tmp_path, monkeypatch):
    from app.services import live_edit_pipeline as pipeline
    class FakeAgent:
        def __init__(self, **kwargs):
            pass

        def detect_fillers(self, candidates, **kwargs):
            assert all(c["end"] <= 5 for c in candidates)
            return candidates

    monkeypatch.setattr(pipeline, "LLMAnalysisService", FakeAgent)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    segments = sample() + [{"start": 10, "end": 10.3, "text": "네."}]
    cuts, _ = filler_candidates(segments)
    plan = {"source_video_path": str(source), "criteria_prompt": "ai_news",
            "candidates": [{"segment_id": "one", "start": 0, "end": 5, "text": segments[0]["text"]}],
            "script_segments": segments, "filler_cuts": cuts}

    def render(_source, clips, output, subtitles, **kwargs):
        assert len(clips) == 3
        assert "네." not in subtitles.read_text(encoding="utf-8")
        output.write_bytes(b"edited")
        return "CPU"

    monkeypatch.setattr(pipeline, "render_final", render)
    result = LiveEditPipeline(tmp_path).rerender_from_selection("job", ["one"], plan=plan)
    assert result["selected_duration_seconds"] == pytest.approx(4.6)
    assert result["filler_summary"]["removed_count"] == 2
    saved = json.loads((tmp_path / "yt-edit/job/job.analysis-plan.json").read_text(encoding="utf-8"))
    assert len(saved["clips"]) == 3
    # 같은 선택으로 재렌더링해도 원본에서 다시 계산하여 중복 차감하지 않는다.
    result = LiveEditPipeline(tmp_path).rerender_from_selection("job", ["one"], plan=plan)
    assert result["selected_duration_seconds"] == pytest.approx(4.6)
    plan["criteria_prompt"] = "game"
    monkeypatch.setattr(pipeline, "render_final", lambda s, c, o, *a, **kw: o.write_bytes(b"edited"))
    result = LiveEditPipeline(tmp_path).rerender_from_selection("job", ["one"], plan=plan)
    assert result["selected_duration_seconds"] == 5


def test_real_ffmpeg_microcuts_keep_audio_video_duration_aligned(tmp_path, monkeypatch):
    from app.services import live_edit_pipeline as pipeline
    tools = Path(__file__).resolve().parents[1] / "bin"
    ffmpeg, ffprobe = tools / "ffmpeg.exe", tools / "ffprobe.exe"
    if not ffmpeg.is_file() or not ffprobe.is_file():
        pytest.skip("로컬 FFmpeg 도구가 필요합니다.")
    folder = tmp_path / ("작업" + "x" * max(1, 145 - len(str(tmp_path)) - 3))
    folder.mkdir()
    source = tmp_path / "source.mp4"
    output = folder / f"Xxuv8AkPS4Y.1a09a4a89ff.edited.{'a' * 32}.pending.mp4"
    # 컷마다 비디오 1프레임보다 작은 단위 오차가 누적되는지 실제로 확인한다.
    subprocess.run([str(ffmpeg), "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=30:duration=6",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=6",
                    "-c:v", "libx264", "-c:a", "aac", "-y", str(source)], check=True, capture_output=True)
    clips = [{"start": i * 0.3, "end": i * 0.3 + 0.247, "filler_edited": True} for i in range(20)]
    expected = sum(c["end"] - c["start"] for c in clips)
    subtitles = tmp_path / "cut.srt"
    write_selected_subtitles([{"start": 0, "end": 6, "text": "핵심 내용"}], clips, subtitles)
    monkeypatch.setattr(pipeline, "_ffmpeg_binary", lambda: str(ffmpeg))
    monkeypatch.setattr(pipeline, "_render_encoder_candidates", lambda: (None,))
    pipeline.render_final(source, clips, output, subtitles)
    probe = subprocess.run([str(ffprobe), "-v", "error", "-show_streams", "-of", "json", str(output)],
                           check=True, capture_output=True, text=True, encoding="utf-8")
    streams = {s["codec_type"]: s for s in json.loads(probe.stdout)["streams"]}
    assert abs(float(streams["audio"]["duration"]) - expected) < 0.04
    assert abs(float(streams["video"]["duration"]) - expected) < 0.07
    assert not list(folder.glob("*.filters.txt"))
    assert not list(folder.glob("render-*.mp4"))


def test_render_long_job_path_uses_short_temporary_names(tmp_path, monkeypatch):
    from app.services import live_edit_pipeline as pipeline
    # 작업 폴더와 최종 경로 자체는 유효하지만 예전 중첩 임시 이름은 260자를 넘는다.
    folder = tmp_path / ("작업" + "x" * max(1, 145 - len(str(tmp_path)) - 3))
    folder.mkdir()
    job = "Xxuv8AkPS4Y.1a09a4a89ff"
    output = folder / f"{job}.edited.{'a' * 32}.pending.mp4"
    old_script = folder / f"{output.stem}.h264_nvenc.{'b' * 32}.attempt.{'c' * 32}.filters.txt"
    assert len(str(output)) < 260 < len(str(old_script))
    monkeypatch.setattr(pipeline, "_render_encoder_candidates", lambda: ("h264_nvenc", None))
    monkeypatch.setattr(pipeline, "_ffmpeg_binary", lambda: "ffmpeg")
    paths = []
    real_copy = pipeline.shutil.copyfile

    def copy(source, destination):
        assert len(str(source)) < 260 and len(str(destination)) < 260
        return real_copy(source, destination)

    def run(command, **kwargs):
        script = Path(command[command.index("-/filter_complex") + 1])
        attempt = Path(command[-1])
        assert len(str(script)) < 260 and len(str(attempt)) < 260
        assert "atrim" in script.read_text(encoding="utf-8")
        paths.extend([script, attempt])
        attempt.write_bytes(b"partial")
        if "h264_nvenc" in command:
            raise pipeline.LiveEditPipelineError("GPU unavailable")
        attempt.write_bytes(b"rendered")

    monkeypatch.setattr(pipeline.shutil, "copyfile", copy)
    monkeypatch.setattr(pipeline, "_run_ffmpeg", run)
    backend = pipeline.render_final(folder / "source.mp4", [
        {"start": 0, "end": 1, "filler_edited": True}], output)
    assert backend == "CPU (libx264)"
    assert output.read_bytes() == b"rendered"
    assert all(not path.exists() for path in paths)
    assert not list(folder.glob("*.tmp"))

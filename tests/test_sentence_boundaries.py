import copy
import json
import threading

import pytest

from app.services.filler_edit import filler_candidates
from app.services.llm_analysis_service import LLMAnalysisService
from app.services.sentence_boundaries import sentence_spans, split_timed_sentences


@pytest.mark.parametrize("text,expected", [
    ("네. 드디어 나왔습니다. 그래서 전반적으로 보면 좋습니다.",
     ["네.", "드디어 나왔습니다.", "그래서 전반적으로 보면 좋습니다."]),
    ("GPT 5.6. 버전 1.2.3입니다.", ["GPT 5.6.", "버전 1.2.3입니다."]),
    ("네.드디어 나왔습니다.", ["네.", "드디어 나왔습니다."]),
    ('"시작합니다." 다음입니다.', ['"시작합니다."', "다음입니다."]),
    ("맞나요? 맞습니다! 다음。 끝", ["맞나요?", "맞습니다!", "다음。", "끝"]),
    ("마침표 없는 마지막 문장", ["마침표 없는 마지막 문장"]),
    ("  ", []),
])
def test_punctuation_sentences(text, expected):
    assert [text[a:b] for a, b in sentence_spans(text)] == expected


def test_sentence_spans_cross_original_cue_boundaries_and_keep_original():
    rows = [
        {"start": 0, "end": 1, "text": "전반적으로 보면"},
        {"start": 1, "end": 3, "text": "성능이 좋습니다."},
        {"start": 3, "end": 4, "text": "다음 소식"},
    ]
    original = copy.deepcopy(rows)
    result = split_timed_sentences(rows)
    assert [r["text"] for r in result] == ["전반적으로 보면 성능이 좋습니다.", "다음 소식"]
    assert [(r["start"], r["end"]) for r in result] == [(0, 3), (3, 4)]
    assert not any(r["timing_estimated"] for r in result)
    assert result[0]["source_segment_ids"] == [0, 1]
    assert rows == original


def test_word_timestamps_define_non_overlapping_boundaries():
    rows = [{"start": 0, "end": 5, "text": "네. 반갑습니다. 다음",
             "words": [
                 {"word": "네.", "start": 0, "end": 0.3},
                 {"word": "반갑습니다.", "start": 0.5, "end": 2},
                 {"word": "다음", "start": 3, "end": 5},
             ]}]
    result = split_timed_sentences(rows)
    assert [(r["start"], r["end"]) for r in result] == [(0, 0.4), (0.4, 2.5), (2.5, 5)]
    assert not any(r["timing_estimated"] for r in result)
    assert [w for r in result for w in r["words"]] == rows[0]["words"]


def test_missing_word_timing_is_estimated_and_not_used_for_filler_cuts():
    result = split_timed_sentences([{"start": 0, "end": 1, "text": "네. 다음."}])
    assert all(r["timing_estimated"] for r in result)
    assert result[0]["end"] == result[1]["start"]
    assert result[0]["start"] == 0 and result[-1]["end"] == 1
    assert all(r["words"] == [] for r in result)
    cuts, missing = filler_candidates(result)
    assert cuts == [] and missing == 2


def test_punctuation_inside_single_timed_word_is_marked_estimated():
    result = split_timed_sentences([{"start": 0, "end": 4, "text": "네.다음.",
                                   "words": [{"word": "네.다음.", "start": 0, "end": 4}]}])
    assert len(result) == 2
    assert all(r["timing_estimated"] for r in result)
    assert result[0]["end"] == result[1]["start"]


@pytest.mark.parametrize("start,end", [(1, 0), (0, 0), (float("nan"), 1)])
def test_invalid_times_are_rejected(start, end):
    with pytest.raises(ValueError):
        split_timed_sentences([{"start": start, "end": end, "text": "문장."}])


def test_chapters_use_llm_but_sentence_sections_do_not():
    class Gateway:
        calls = 0

        def request_json(self, *args, **kwargs):
            self.calls += 1
            return json.dumps({"chapters": [{"start_id": 0, "end_id": 2, "summary": "뉴스", "score": 800}]})

    agent = object.__new__(LLMAnalysisService)
    agent.gateway = Gateway()
    agent._checkpoint_responses = {}
    agent._checkpoint_lock = threading.Lock()
    rows = split_timed_sentences([{"start": 0, "end": 5, "text": "네. GPT 5.6. 드디어 나왔습니다."}])
    first = agent.structure_transcript(rows)
    second = agent.structure_transcript(rows)
    assert first == second
    assert first["sections"] == [
        {"chapter_index": 0, "start_id": i, "end_id": i} for i in range(3)
    ]
    assert agent.gateway.calls == 1


def test_uploader_chapters_keep_sentence_sections_without_llm():
    agent = object.__new__(LLMAnalysisService)
    rows = split_timed_sentences([{"start": 0, "end": 5, "text": "첫째. 둘째."}])
    chapters = [
        {"start_id": 0, "end_id": 0, "summary": "첫째", "score": 500},
        {"start_id": 1, "end_id": 1, "summary": "둘째", "score": 500},
    ]
    result = agent.structure_transcript(rows, chapters=chapters)
    assert result["chapters"] == chapters
    assert [s["chapter_index"] for s in result["sections"]] == [0, 1]

import copy
import json
import re
import threading

import pytest

from app.services.llm_analysis_service import LLMAnalysisError, LLMAnalysisService
from app.services.sentence_boundaries import split_timed_sentences


def make_agent(respond):
    calls = []

    class Gateway:
        def request_json(self, system, prompt, **kwargs):
            data = json.loads(prompt)
            calls.append(data)
            return json.dumps(respond(data, kwargs["response_schema"]))

    agent = object.__new__(LLMAnalysisService)
    agent.gateway = Gateway()
    agent._checkpoint_responses = {}
    agent._checkpoint_lock = threading.Lock()
    return agent, calls


def test_twelve_minute_unpunctuated_cooking_transcript_keeps_text_and_times():
    rows = [{"id": i, "start": i * 6, "end": (i + 1) * 6,
             "text": f"재료{i} 넣습니다"} for i in range(120)]
    original = copy.deepcopy(rows)
    agent, calls = make_agent(lambda data, schema: {
        "end_ids": [word["id"] for word in data["words"] if word["text"] == "넣습니다"]
    })
    result = split_timed_sentences(rows, semantic_splitter=agent.split_unpunctuated_sentences)
    assert len(result) == 120
    assert [(s["start"], s["end"], s["text"]) for s in result] == [
        (s["start"], s["end"], s["text"]) for s in rows
    ]
    assert rows == original
    assert not any(s["timing_estimated"] for s in result)
    assert len(calls) == 1
    assert split_timed_sentences(rows, semantic_splitter=agent.split_unpunctuated_sentences) == result
    assert len(calls) == 1  # 검증된 응답 재사용


def test_partial_sentence_is_carried_across_request_windows_without_word_loss():
    def respond(data, schema):
        maximum = schema["properties"]["end_ids"]["items"]["maximum"]
        return {"end_ids": [word["id"] for word in data["words"]
                            if (word["id"] + 1) % 3 == 0 and word["id"] <= maximum]}

    agent, calls = make_agent(respond)
    text = " ".join(f"단어{i}" for i in range(510))
    ends = agent.split_unpunctuated_sentences(text)
    tokens = list(re.finditer(r"\S+", text))
    assert ends == [tokens[i].end() for i in range(2, 510, 3)]
    assert calls[0]["words"][-1]["id"] == 239
    assert calls[1]["words"][0]["id"] == 237
    assert calls[-1]["is_final"] is True


def test_incomplete_window_expands_instead_of_forcing_a_cut():
    def respond(data, schema):
        return {"end_ids": [data["words"][-1]["id"]] if data["is_final"] else []}

    agent, calls = make_agent(respond)
    text = " ".join(["단어"] * 300)
    assert agent.split_unpunctuated_sentences(text) == [len(text)]
    assert [len(c["words"]) for c in calls] == [240, 300]


def test_no_safe_boundary_in_long_nonfinal_input_pauses():
    agent, calls = make_agent(lambda data, schema: {"end_ids": []})
    with pytest.raises(LLMAnalysisError, match="문장 경계를 찾지 못했습니다"):
        agent.split_unpunctuated_sentences(" ".join(["단어"] * 1000))
    assert [len(c["words"]) for c in calls] == [240, 480, 960]


@pytest.mark.parametrize("ends", [[0], [1, 0], [1, 1], [2], ["1"], [True], None])
def test_invalid_boundary_contract_is_retried_but_not_cached(ends):
    agent, calls = make_agent(lambda data, schema: {"end_ids": ends})
    with pytest.raises(LLMAnalysisError, match="검증이 3회 실패"):
        agent.split_unpunctuated_sentences("첫째 둘째")
    assert len(calls) == 3
    assert not agent._checkpoint_responses


def test_semantic_word_cuts_keep_measured_alignment():
    rows = [{"start": 0, "end": 5, "text": "양파를 썰어요 팬을 달궈요", "words": [
        {"word": "양파를", "start": 0, "end": 1},
        {"word": "썰어요", "start": 1, "end": 2},
        {"word": "팬을", "start": 3, "end": 4},
        {"word": "달궈요", "start": 4, "end": 5},
    ]}]
    result = split_timed_sentences(rows, semantic_splitter=lambda text: [7, len(text)])
    assert [s["text"] for s in result] == ["양파를 썰어요", "팬을 달궈요"]
    assert [(s["start"], s["end"]) for s in result] == [(0, 2.5), (2.5, 5)]
    assert not any(s["timing_estimated"] for s in result)
    assert [w for s in result for w in s["words"]] == rows[0]["words"]


def test_missing_word_times_are_marked_estimated():
    result = split_timed_sentences(
        [{"start": 0, "end": 5, "text": "양파를 썰어요 팬을 달궈요"}],
        semantic_splitter=lambda text: [7, len(text)],
    )
    assert all(s["timing_estimated"] for s in result)
    assert result[0]["end"] == result[1]["start"]


def test_punctuated_sentences_stay_fixed_and_only_tail_needs_llm():
    calls = []

    def split(text):
        calls.append(text)
        return [len(text)]

    result = split_timed_sentences(
        [{"start": 0, "end": 20, "text": "GPT 5.6입니다. 이제 시작해요"}],
        semantic_splitter=split,
    )
    assert calls == ["이제 시작해요"]
    assert [s["text"] for s in result] == ["GPT 5.6입니다.", "이제 시작해요"]


def test_sparse_punctuation_also_gets_semantic_review():
    calls = []
    text = "긴단어 " * 40 + "끝입니다."

    def split(value):
        calls.append(value)
        return [len(value)]

    split_timed_sentences([{"start": 0, "end": 60, "text": text}], semantic_splitter=split)
    assert calls == [text]


def test_cancellation_stops_before_call():
    agent, calls = make_agent(lambda data, schema: {"end_ids": [1]})

    def cancel():
        raise RuntimeError("취소됨")

    with pytest.raises(RuntimeError, match="취소됨"):
        agent.split_unpunctuated_sentences("첫째 둘째", cancel_callback=cancel)
    assert calls == []


@pytest.mark.parametrize("ends", [[1], [100], [7, 7, 14], []])
def test_mapper_rejects_invalid_or_incomplete_character_boundaries(ends):
    with pytest.raises(ValueError, match="문장 경계"):
        split_timed_sentences(
            [{"start": 0, "end": 5, "text": "양파를 썰어요 팬을 달궈요"}],
            semantic_splitter=lambda text: ends,
        )

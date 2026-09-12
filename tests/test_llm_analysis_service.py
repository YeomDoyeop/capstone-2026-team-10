import json

import pytest

from app.services.llm_analysis_service import LLMAnalysisError, LLMAnalysisService
from app.services.prompt_store import list_user_prompts, system_prompt

CHAPTER_SYSTEM = system_prompt("chapter")
SECTION_SYSTEM = system_prompt("section")
SUBTITLE_SPLIT_SYSTEM = system_prompt("subtitle_split")
WHISPER_SETTINGS_SYSTEM = system_prompt("whisper_settings")
GENRE_GUIDES = {
    item["id"]: str(item.get("criteria") or "") for item in list_user_prompts()
}


def test_prompts_define_summary_and_precise_score_contract():
    prompt = f"{CHAPTER_SYSTEM}\n{SECTION_SYSTEM}\n" + "\n".join(GENRE_GUIDES.values())
    assert "Gemini" not in prompt and "DeepSeek" not in prompt
    assert "summary" in CHAPTER_SYSTEM and "title" not in CHAPTER_SYSTEM
    assert "summary" not in SECTION_SYSTEM and "reason" not in SECTION_SYSTEM
    assert "균등 분할" in CHAPTER_SYSTEM and "문장마다 기계적으로" in SECTION_SYSTEM
    assert "애매하면 나누지 않고 큰 챕터" in CHAPTER_SYSTEM
    assert "가능한 한 작은 의미 단위" in SECTION_SYSTEM
    assert "질문과 답변" in CHAPTER_SYSTEM and "질문과 답변" in SECTION_SYSTEM


def test_whisper_settings_uses_strict_json_and_keeps_proper_nouns_as_hotwords(
    monkeypatch,
):
    agent = object.__new__(LLMAnalysisService)
    captured: dict = {}

    def request(system, prompt, **kwargs):
        captured.update({"system": system, "prompt": json.loads(prompt), **kwargs})
        return kwargs["validator"](
            {
                "hotwords": [
                    {"text": "AVE", "entity_type": "service"},
                    {"text": "OpenAI", "entity_type": "organization"},
                    {"text": "AVE", "entity_type": "service"},
                ]
            }
        )

    monkeypatch.setattr(agent, "_request_json", request)
    result = agent.recommend_whisper_settings(
        {
            "title": "AVE와 OpenAI",
            "description": "",
            "chapters": [],
            "channel": "",
            "categories": [],
            "tags": [],
        }
    )
    assert (
        "고유명사" in WHISPER_SETTINGS_SYSTEM
        and "JSON 외 텍스트" in WHISPER_SETTINGS_SYSTEM
    )
    assert (
        "특정 대상" in WHISPER_SETTINGS_SYSTEM and "고유명사" in WHISPER_SETTINGS_SYSTEM
    )
    assert (
        "개발자" not in WHISPER_SETTINGS_SYSTEM
        and "인공지능" not in WHISPER_SETTINGS_SYSTEM
    )
    assert captured["prompt"]["title"] == "AVE와 OpenAI"
    assert captured["response_schema"]["additionalProperties"] is False
    assert result == {"hotwords": "AVE, OpenAI"}


def test_whisper_settings_rejects_hotword_not_present_in_metadata():
    try:
        LLMAnalysisService._validated_whisper_settings(
            {"hotwords": [{"text": "추측한 회사명", "entity_type": "organization"}]},
            metadata_text='{"title":"AVE 인터뷰"}',
        )
    except LLMAnalysisError as exc:
        assert "원문 표기" in str(exc)
    else:
        raise AssertionError("메타데이터에 없는 핫워드를 허용하면 안 됩니다.")


def test_whisper_settings_rejects_unclassified_hotword():
    try:
        LLMAnalysisService._validated_whisper_settings(
            {"hotwords": [{"text": "고유명사하나"}]},
            metadata_text="고유명사하나",
        )
    except LLMAnalysisError as exc:
        assert "개체 유형" in str(exc)
    else:
        raise AssertionError("개체 유형 없는 핫워드를 허용하면 안 됩니다.")


def test_subtitle_split_requires_contiguous_word_ranges(monkeypatch):
    agent = object.__new__(LLMAnalysisService)
    captured = {}

    def request(system, prompt, **kwargs):
        captured["system"] = system
        captured["prompt"] = json.loads(prompt)
        return kwargs["validator"]({"indexes": [1]})

    monkeypatch.setattr(agent, "_request_json", request)
    result = agent.split_subtitle_words(
        [{"word": value} for value in ["하나", " 둘", " 셋", " 넷"]]
    )

    assert result[-1]["end_word"] == 3
    assert set(captured["prompt"]) == {"words"}
    assert "목표 개수는 주어지지 않는다" in SUBTITLE_SPLIT_SYSTEM
    assert "공백 제외 약 30자" in SUBTITLE_SPLIT_SYSTEM
    assert "자막" in SUBTITLE_SPLIT_SYSTEM


def test_subtitle_split_converts_boundary_indexes_to_contiguous_ranges(monkeypatch):
    agent = object.__new__(LLMAnalysisService)
    monkeypatch.setattr(
        agent,
        "_request_json",
        lambda *_args, **kwargs: kwargs["validator"]({"indexes": [1, 3]}),
    )

    result = agent.split_subtitle_words(
        [
            {"word": value}
            for value in ["가나다", "라마", "바사", "아자", "차카", "타파하"]
        ],
    )

    assert result == [
        {"start_word": 0, "end_word": 1},
        {"start_word": 2, "end_word": 3},
        {"start_word": 4, "end_word": 5},
    ]


def test_subtitle_split_accepts_no_boundary_when_llm_cannot_split(monkeypatch):
    agent = object.__new__(LLMAnalysisService)
    monkeypatch.setattr(
        agent,
        "_request_json",
        lambda *_args, **kwargs: kwargs["validator"]({"indexes": []}),
    )

    result = agent.split_subtitle_words(
        [{"word": value} for value in ["나누기", "어려운", "자막"]]
    )

    assert result == [{"start_word": 0, "end_word": 2}]


def test_structure_requires_contiguous_ids_and_summaries(monkeypatch):
    agent = object.__new__(LLMAnalysisService)
    responses = iter(
        [
            {
                "chapters": [
                    {"start_id": 0, "end_id": 1, "summary": "주제 요약", "score": 812}
                ]
            },
            {"sections": [{"start_id": 0, "end_id": 1}]},
        ]
    )
    monkeypatch.setattr(
        agent,
        "_request_json",
        lambda *args, **kwargs: kwargs["validator"](next(responses)),
    )
    result = agent.structure_transcript(
        [{"id": 0, "text": "가"}, {"id": 1, "text": "나"}]
    )
    assert result["chapters"][0]["summary"] == "주제 요약"
    assert result["sections"][0] == {"chapter_index": 0, "start_id": 0, "end_id": 1}


def test_score_sends_one_section_id_text_array_and_chapter_summary_per_chapter(
    monkeypatch,
):
    agent = object.__new__(LLMAnalysisService)
    prompts = []
    def request(_system, prompt, **_kwargs):
        parsed = json.loads(prompt)
        prompts.append(parsed)
        base = 903 if parsed["chapter_summary"] == "둘째 챕터 요약" else 721
        return {
            "items": [
                {"id": item["id"], "score": base + item["id"] * 93}
                for item in parsed["sections"]
            ]
        }

    monkeypatch.setattr(agent, "_request_json", request)
    result = agent.score_sections(
        [
            {
                "chapter_id": "a",
                "chapter_summary": "첫 챕터 요약",
                "section_id": "a-1",
                "text": "첫 섹션",
            },
            {
                "chapter_id": "a",
                "chapter_summary": "첫 챕터 요약",
                "section_id": "a-2",
                "text": "둘째 섹션",
            },
            {
                "chapter_id": "b",
                "chapter_summary": "둘째 챕터 요약",
                "section_id": "b-1",
                "text": "다른 챕터",
            },
        ]
    )
    criteria_profiles = [prompt.pop("criteria_profile") for prompt in prompts]
    assert [profile["id"] for profile in criteria_profiles] == ["ai_news", "ai_news"]
    assert all(profile["criteria"] for profile in criteria_profiles)
    prompts.sort(key=lambda prompt: prompt["chapter_summary"], reverse=True)
    assert prompts == [
        {
            "chapter_summary": "첫 챕터 요약",
            "sections": [
                {"id": 0, "text": "첫 섹션"},
                {"id": 1, "text": "둘째 섹션"},
            ],
        },
        {
            "chapter_summary": "둘째 챕터 요약",
            "sections": [{"id": 0, "text": "다른 챕터"}],
        },
    ]
    assert [item["llm_score"] for item in result] == [0.721, 0.814, 0.903]
    assert all("reason" not in item for item in result)


def test_timestamp_comments_are_scored_in_one_indexed_request(monkeypatch):
    agent = object.__new__(LLMAnalysisService)
    captured = {}

    def request(system, prompt, **kwargs):
        captured["system"] = system
        captured["prompt"] = json.loads(prompt)
        captured["schema"] = kwargs["response_schema"]
        return kwargs["validator"](
            {"items": [{"index": 0, "score": 0}, {"index": 1, "score": 847}]}
        )

    monkeypatch.setattr(agent, "_request_json", request)
    result = agent.score_timestamp_comments(
        [
            {"text": "00:10 도입부"},
            {"text": "1:20 여기 정말 재미있다"},
        ]
    )

    assert captured["prompt"] == {
        "comments": [
            {"index": 0, "text": "00:10 도입부"},
            {"index": 1, "text": "1:20 여기 정말 재미있다"},
        ]
    }
    assert result == [{"index": 0, "score": 0.0}, {"index": 1, "score": 0.847}]
    assert "챕터 표기" in captured["system"]
    assert "타임스탬프가 여러 개" in captured["system"]
    assert captured["schema"]["required"] == ["items"]


def test_score_rejects_wrong_score_count(monkeypatch):
    agent = object.__new__(LLMAnalysisService)
    monkeypatch.setattr(
        agent,
        "_request_json",
        lambda *_args, **_kwargs: {"items": [{"id": 0, "score": 900}]},
    )
    try:
        agent.score_sections(
            [
                {"chapter_id": "a", "section_id": "a-1", "text": "첫"},
                {"chapter_id": "a", "section_id": "a-2", "text": "둘째"},
            ]
        )
    except LLMAnalysisError as exc:
        assert "일치" in str(exc)
    else:
        raise AssertionError("wrong score count must be rejected")


def test_request_json_retries_after_range_error():
    class Gateway:
        def __init__(self):
            self.calls = 0

        def request_json(self, *_args, **_kwargs):
            self.calls += 1
            return (
                '{"sections":[{"start_id":1,"end_id":2}]}'
                if self.calls == 1
                else '{"sections":[{"start_id":0,"end_id":2}]}'
            )

    agent = object.__new__(LLMAnalysisService)
    agent.gateway = Gateway()
    result = agent._request_json(
        SECTION_SYSTEM,
        "입력",
        validator=lambda raw: agent._validated_ranges(
            raw, [0, 1, 2], key="sections", require_chapter_fields=False
        ),
    )
    assert result[0]["start_id"] == 0 and agent.gateway.calls == 2


def test_request_json_stops_after_one_hundred_invalid_responses():
    class Gateway:
        def __init__(self):
            self.calls = 0

        def request_json(self, *_args, **_kwargs):
            self.calls += 1
            return '{"broken":'

    agent = object.__new__(LLMAnalysisService)
    agent.gateway = Gateway()
    try:
        agent._request_json("계약", "입력")
    except LLMAnalysisError as exc:
        assert "백 번" in str(exc)
    else:
        raise AssertionError("ten invalid responses must fail")
    assert agent.gateway.calls == 100


def test_request_json_returns_last_contract_failure_reason():
    class Gateway:
        def request_json(self, *_args, **_kwargs):
            return '{"value":1}'

    agent = object.__new__(LLMAnalysisService)
    agent.gateway = Gateway()

    try:
        agent._request_json(
            "계약",
            "입력",
            validator=lambda _raw: (_ for _ in ()).throw(
                LLMAnalysisError("ids 시간순 오류")
            ),
        )
    except LLMAnalysisError as exc:
        assert "ids 시간순 오류" in str(exc)
    else:
        raise AssertionError("last validation reason must be exposed")


def test_anchor_links_use_index_array_contract(monkeypatch):
    agent = object.__new__(LLMAnalysisService)
    captured = {}

    def request(_system, _prompt, **kwargs):
        captured["schema"] = kwargs["response_schema"]
        return kwargs["validator"]({"indexes": [0, 2]})

    monkeypatch.setattr(agent, "_request_json", request)

    result = agent.required_anchor_links(
        "s1",
        "요약",
        [
            {"id": "s0", "text": "앞"},
            {"id": "s1", "text": "핵심"},
            {"id": "s2", "text": "뒤"},
        ],
    )

    assert result == ["s0", "s2"]
    assert captured["schema"]["required"] == ["indexes"]
    indexes_schema = captured["schema"]["properties"]["indexes"]
    assert indexes_schema["items"] == {"type": "integer"}
    assert indexes_schema["uniqueItems"] is True


def test_anchor_links_skip_llm_when_chapter_has_no_other_section(monkeypatch):
    agent = object.__new__(LLMAnalysisService)
    monkeypatch.setattr(
        agent,
        "_request_json",
        lambda *_args, **_kwargs: pytest.fail("후보가 없으면 LLM을 호출하면 안 됩니다."),
    )

    assert agent.required_anchor_links(
        "s1", "요약", [{"id": "s1", "text": "핵심"}]
    ) == []


def test_anchor_links_allow_returned_anchor_id(monkeypatch):
    agent = object.__new__(LLMAnalysisService)

    def request(_system, _prompt, **kwargs):
        return kwargs["validator"]({"indexes": [1]})

    monkeypatch.setattr(agent, "_request_json", request)
    assert agent.required_anchor_links(
        "s1",
        "요약",
        [{"id": "s0", "text": "앞"}, {"id": "s1", "text": "핵심"}],
    ) == ["s1"]


def test_chapter_score_requires_an_integer_in_range():
    raw = {"chapters": [{"start_id": 0, "end_id": 1, "summary": "요약", "score": 1001}]}

    try:
        LLMAnalysisService._validated_ranges(
            raw, [0, 1], key="chapters", require_chapter_fields=True
        )
    except LLMAnalysisError as exc:
        assert "score" in str(exc)
    else:
        raise AssertionError("out-of-range chapter score must be rejected")


def test_structure_rejects_extra_response_fields():
    raw = {
        "chapters": [
            {
                "start_id": 0,
                "end_id": 0,
                "summary": "요약",
                "score": 500,
                "reason": "금지",
            }
        ]
    }

    try:
        LLMAnalysisService._validated_ranges(
            raw, [0], key="chapters", require_chapter_fields=True
        )
    except LLMAnalysisError as exc:
        assert "필드" in str(exc)
    else:
        raise AssertionError("extra chapter fields must be rejected")


def test_request_json_checks_cancellation_before_submitting_a_request():
    class Gateway:
        def request_json(self, *_args, **_kwargs):
            raise AssertionError("cancelled work must not submit an LLM request")

    agent = object.__new__(LLMAnalysisService)
    agent.gateway = Gateway()

    try:
        agent._request_json(
            "계약",
            "입력",
            cancel_callback=lambda: (_ for _ in ()).throw(LLMAnalysisError("취소됨")),
        )
    except LLMAnalysisError as exc:
        assert str(exc) == "취소됨"
    else:
        raise AssertionError("cancellation must stop the request")


def test_request_json_discards_a_completed_request_when_it_was_cancelled(monkeypatch):
    class Gateway:
        def request_json(self, *_args, **_kwargs):
            return '{"sections":[{"start_id":0,"end_id":0}]}'

    agent = object.__new__(LLMAnalysisService)
    agent.gateway = Gateway()
    checks = 0

    def cancel_after_request():
        nonlocal checks
        checks += 1
        if checks >= 2:
            raise LLMAnalysisError("취소됨")

    try:
        agent._request_json(
            SECTION_SYSTEM, "입력", cancel_callback=cancel_after_request
        )
    except LLMAnalysisError as exc:
        assert str(exc) == "취소됨"
    else:
        raise AssertionError("completed response must be discarded after cancellation")


def test_parallel_section_work_never_requests_more_than_ten_workers(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor as RealExecutor
    import app.services.llm_analysis_service as service_module

    requested_workers = []

    class RecordingExecutor(RealExecutor):
        def __init__(self, *args, **kwargs):
            requested_workers.append(
                kwargs.get("max_workers", args[0] if args else None)
            )
            super().__init__(*args, **kwargs)

    agent = object.__new__(LLMAnalysisService)
    monkeypatch.setattr(service_module, "ThreadPoolExecutor", RecordingExecutor)
    monkeypatch.setattr(
        agent,
        "_request_json",
        lambda *_args, **_kwargs: {"items": [{"id": 0, "score": 500}]},
    )

    agent.score_sections(
        [
            {"chapter_id": f"chapter-{index}", "section_id": "section", "text": "본문"}
            for index in range(101)
        ]
    )

    assert requested_workers == [100]


def test_parallel_chapter_splitting_never_requests_more_than_ten_workers(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor as RealExecutor
    import app.services.llm_analysis_service as service_module

    requested_workers = []

    class RecordingExecutor(RealExecutor):
        def __init__(self, *args, **kwargs):
            requested_workers.append(
                kwargs.get("max_workers", args[0] if args else None)
            )
            super().__init__(*args, **kwargs)

    agent = object.__new__(LLMAnalysisService)
    monkeypatch.setattr(service_module, "ThreadPoolExecutor", RecordingExecutor)

    def request(system, _prompt, **kwargs):
        if system == service_module.system_prompt("chapter"):
            raw = {
                "chapters": [
                    {
                        "start_id": index,
                        "end_id": index,
                        "summary": str(index),
                        "score": 500,
                    }
                    for index in range(101)
                ]
            }
        else:
            start = int(_prompt.split('"id":')[1].split(",")[0])
            raw = {"sections": [{"start_id": start, "end_id": start}]}
        validator = kwargs.get("validator")
        return validator(raw) if validator else raw

    monkeypatch.setattr(agent, "_request_json", request)
    agent.structure_transcript(
        [{"id": index, "text": str(index)} for index in range(101)]
    )

    assert requested_workers == [100]

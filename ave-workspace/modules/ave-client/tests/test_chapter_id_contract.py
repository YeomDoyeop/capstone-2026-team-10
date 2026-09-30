import json
import threading

import pytest

from app.services.llm_analysis_service import LLMAnalysisError, LLMAnalysisService
from app.services.prompt_store import response_schema


def chapter(start, end):
    return {"start_id": start, "end_id": end, "summary": "주제", "score": 500}


def validate(items):
    return LLMAnalysisService._validated_ranges(
        {"chapters": items}, [10, 11, 12], key="chapters", require_chapter_fields=True
    )


@pytest.mark.parametrize("start,end,reason", [
    (9, 12, "입력에 없는 ID"), (10, 13, "입력에 없는 ID"),
    (12, 10, "start_id는 end_id 이하"),
    ("10", 12, "JSON 정수"), (10.0, 12, "JSON 정수"),
    (None, 12, "JSON 정수"), (True, 12, "JSON 정수"),
])
def test_invalid_ids_explain_item_range_and_reason(start, end, reason):
    with pytest.raises(LLMAnalysisError) as error:
        validate([chapter(start, end)])
    assert "chapters[0]" in str(error.value)
    assert "허용 ID=10~12" in str(error.value)
    assert reason in str(error.value)


def test_id_diagnostic_does_not_expose_arbitrary_response_text():
    with pytest.raises(LLMAnalysisError) as error:
        validate([chapter("secret arbitrary response text", 12)])
    assert "secret" not in str(error.value)
    assert "<str>" in str(error.value)


@pytest.mark.parametrize("items,detail", [
    ([chapter(10, 10), chapter(12, 12)], "start_id는 11"),
    ([chapter(10, 11), chapter(11, 12)], "start_id는 12"),
    ([chapter(10, 11)], "필요한 end_id=12"),
])
def test_coverage_error_explains_required_boundary(items, detail):
    with pytest.raises(LLMAnalysisError, match=detail):
        validate(items)


def test_dynamic_schema_is_request_specific_and_preserves_sentence_sections():
    agent = object.__new__(LLMAnalysisService)
    schemas = []

    def request(system, prompt, **kwargs):
        rows = [json.loads(line) for line in prompt.splitlines()]
        first, last = rows[0]["id"], rows[-1]["id"]
        assert f"허용 ID={first}~{last}" in system
        assert "시간(초)이 아니라" in system
        schemas.append(kwargs["response_schema"])
        return kwargs["validator"]({"chapters": [chapter(first, last)]})

    agent._request_json = request
    for ids in ([10, 11, 12], [0], list(range(2339))):
        result = agent.structure_transcript([{"id": i, "text": "문장."} for i in ids])
        assert [(s["start_id"], s["end_id"]) for s in result["sections"]] == [(i, i) for i in ids]
    for schema, first, last in zip(schemas, [10, 0, 0], [12, 0, 2338]):
        array = schema["properties"]["chapters"]
        assert array["minItems"] == 1
        assert "maxItems" not in array
        for field in ("start_id", "end_id"):
            prop = array["items"]["properties"][field]
            assert prop["type"] == "integer"
            assert (prop["minimum"], prop["maximum"]) == (first, last)
    assert "minimum" not in response_schema("chapter")["properties"]["chapters"]["items"]["properties"]["start_id"]


@pytest.mark.parametrize("recover", [True, False])
def test_chapter_retry_contains_diagnostic_and_caches_only_valid_response(recover):
    calls = []

    class Gateway:
        def request_json(self, system, prompt, **kwargs):
            calls.append(system)
            end = 12 if recover and len(calls) > 1 else 99
            return json.dumps({"chapters": [chapter(10, end)]})

    agent = object.__new__(LLMAnalysisService)
    agent.gateway = Gateway()
    agent._checkpoint_responses = {}
    agent._checkpoint_lock = threading.Lock()
    agent._wait_for_request_slot = lambda cancel: None
    rows = [{"id": i, "text": "문장."} for i in (10, 11, 12)]
    if recover:
        result = agent.structure_transcript(rows)
        assert len(result["sections"]) == 3
        assert agent.structure_transcript(rows) == result
        assert len(calls) == 2
        assert len(agent._checkpoint_responses) == 1
    else:
        with pytest.raises(LLMAnalysisError, match="검증이 3회 실패") as error:
            agent.structure_transcript(rows)
        assert "end_id=99" in str(error.value)
        assert len(calls) == 3
        assert not agent._checkpoint_responses
    assert "직전 응답 거부 사유" in calls[1]
    assert "chapters[0]" in calls[1]
    assert "end_id=99" in calls[1]
    assert "허용 ID=10~12" in calls[1]

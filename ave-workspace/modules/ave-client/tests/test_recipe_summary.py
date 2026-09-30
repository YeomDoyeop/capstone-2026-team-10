import asyncio
import json
from zipfile import ZipFile

import pytest
from fastapi import HTTPException

from app.services.recipe_summary import summarize_recipes, save_recipe_files
from app.services.llm_analysis_service import LLMAnalysisError
from app.services.recipe_summary import validate_recipe_response


def recipe(name, ids):
    return {"menu_name": name, "steps": [
        {"stage": "조리", "instruction": f"단계 {i}", "source_ids": [i]} for i in ids
    ], "uncertainties": []}


class Agent:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.names = list(dict.fromkeys(r["menu_name"] for batch in responses for r in batch))
        self.inputs = []

    def _request_json(self, system, prompt, **kwargs):
        payload = json.loads(prompt)
        if payload.get("task") in {"menu_scan", "menu_plan"}:
            source_id = payload["segments"][0]["id"] if payload["task"] == "menu_scan" else 0
            return kwargs["validator"]({"menus": [
                {"name": name, "components": [], "source_ids": [source_id], "reason": "독립 주 요리"}
                for name in self.names
            ], "uncertainties": []})
        self.inputs.append(payload)
        return kwargs["validator"]({"recipes": next(self.responses)})


def rows(count):
    return [{"id": i, "text": "재료를 볶습니다", "start": i * 3, "end": i * 3 + 3} for i in range(count)]


def test_distinct_menus_have_independent_order_and_source_times():
    agent = Agent([[recipe("국", [2, 3]), recipe("볶음", [0, 1])]])
    result = summarize_recipes(agent, rows(4))
    assert [m["menu_name"] for m in result["recipes"]] == ["볶음", "국"]
    assert [m["recipe_index"] for m in result["recipes"]] == [1, 2]
    assert [s["step_index"] for s in result["recipes"][1]["steps"]] == [1, 2]
    assert result["recipes"][1]["steps"][0]["source_ranges"] == [{"segment_id": 2, "start": 6, "end": 9}]


def test_menu_continues_across_batches_without_duplicate_menu():
    agent = Agent([[recipe("볶음", [0])], [recipe("볶음", [80])]])
    result = summarize_recipes(agent, rows(81))
    assert agent.inputs[1]["known_menus"] == ["볶음"]
    assert len(result["recipes"]) == 1
    assert [s["step_index"] for s in result["recipes"][0]["steps"]] == [1, 2]


@pytest.mark.parametrize("bad_id", [999, "0", True])
def test_unknown_or_wrong_type_source_id_is_rejected(bad_id):
    with pytest.raises(LLMAnalysisError):
        summarize_recipes(Agent([[recipe("볶음", [bad_id])]]), rows(2))


def test_empty_transcript_does_not_call_llm():
    agent = Agent([])
    assert summarize_recipes(agent, [])["recipes"] == []
    assert not agent.inputs


@pytest.mark.parametrize("value", [None, "양 확인 필요", "", ["양 확인 필요"], []])
def test_warning_shape_is_normalized_without_losing_text(value):
    import copy
    item = recipe("국", [0])
    item["uncertainties"] = value
    original = copy.deepcopy(item)
    result = validate_recipe_response({"recipes": [item]}, {0})[0]
    assert item == original
    assert isinstance(result["uncertainties"], list)
    if value is None:
        assert "제공하지 않았습니다" in result["uncertainties"][0]
    if value == "양 확인 필요":
        assert result["uncertainties"] == [value]


def test_missing_warning_is_explicitly_marked_for_review():
    item = recipe("국", [0])
    del item["uncertainties"]
    result = validate_recipe_response({"recipes": [item]}, {0})[0]
    assert "원본 확인" in result["uncertainties"][0]


@pytest.mark.parametrize("field,value,path", [
    ("menu_name", "", "recipes[0].menu_name"),
    ("steps", [], "recipes[0].steps"),
    ("steps", "조리 설명", "recipes[0].steps"),
    ("uncertainties", {}, "recipes[0].uncertainties"),
    ("uncertainties", [None], "recipes[0].uncertainties"),
])
def test_diagnostics_identify_invalid_field(field, value, path):
    item = recipe("국", [0])
    item[field] = value
    with pytest.raises(LLMAnalysisError) as error:
        validate_recipe_response({"recipes": [item]}, {0})
    assert path in str(error.value)


def test_unknown_extra_fields_are_not_silently_discarded_or_logged():
    item = recipe("국", [0])
    item["private-extra-name"] = "private-value"
    with pytest.raises(LLMAnalysisError) as error:
        validate_recipe_response({"recipes": [item]}, {0})
    assert "추가 필드" in str(error.value)
    assert "private" not in str(error.value)


@pytest.mark.parametrize("recover", [True, False])
def test_real_retry_path_gets_specific_feedback_and_caches_only_valid(recover, caplog):
    import threading
    from app.services.llm_analysis_service import LLMAnalysisService
    calls = []

    class Gateway:
        def request_json(self, system, prompt, **kwargs):
            if json.loads(prompt).get("task") in {"menu_scan", "menu_plan"}:
                return json.dumps({"menus": [{"name": "국", "components": [], "source_ids": [0], "reason": "주 요리"}], "uncertainties": []})
            calls.append(system)
            item = recipe("국", [0])
            if not recover or len(calls) == 1:
                item["steps"] = []
            return json.dumps({"recipes": [item]}, ensure_ascii=False)

    agent = object.__new__(LLMAnalysisService)
    agent.gateway = Gateway()
    agent._checkpoint_lock = threading.Lock()
    agent._checkpoint_responses = {}
    if recover:
        result = summarize_recipes(agent, rows(1))
        assert len(result["recipes"][0]["steps"]) == 1
        assert summarize_recipes(agent, rows(1)) == result
        assert len(calls) == 2
        assert len(agent._checkpoint_responses) == 3
    else:
        with pytest.raises(LLMAnalysisError, match="검증이 3회 실패") as error:
            summarize_recipes(agent, rows(1))
        assert "recipes[0].steps" in str(error.value)
        assert len(calls) == 3
        assert len(agent._checkpoint_responses) == 2  # 성공한 메뉴 판별만 보존
    assert "직전 응답 거부 사유" in calls[1]
    assert "레시피 묶음 1/1: recipes[0].steps" in calls[1]
    assert "레시피 묶음 1/1: recipes[0].steps" in caplog.text


def test_invalid_source_id_reports_step_path():
    with pytest.raises(LLMAnalysisError) as error:
        validate_recipe_response({"recipes": [recipe("국", [10])]}, {0, 1})
    assert "recipes[0].steps[0].source_ids" in str(error.value)


def test_empty_non_recipe_batch_is_valid():
    assert validate_recipe_response({"recipes": []}, {0}) == []


def test_exports_have_fixed_safe_names_and_numbered_steps(tmp_path):
    document = summarize_recipes(Agent([[recipe("../../국", [0, 1])]]), rows(2))
    save_recipe_files(tmp_path, document)
    assert json.loads((tmp_path / "recipes.json").read_text(encoding="utf-8")) == document
    markdown = (tmp_path / "recipes.md").read_text(encoding="utf-8")
    assert "1. [조리]" in markdown and "2. [조리]" in markdown
    with ZipFile(tmp_path / "recipes.zip") as archive:
        assert set(archive.namelist()) == {"recipes.json", "recipes.md", "01-recipe.json", "01-recipe.md"}


def test_recipe_download_requires_owner_and_handles_missing_file(tmp_path, monkeypatch):
    from app.main import LIVE_EDIT_JOBS, get_edit_media
    LIVE_EDIT_JOBS["recipe-test"] = {"owner_id": "owner", "result": {}}
    monkeypatch.setattr("app.main._edit_output_dir", lambda job_id: tmp_path)
    try:
        with pytest.raises(HTTPException) as error:
            asyncio.run(get_edit_media("recipe-test", "recipes-json", {"id": "other"}))
        assert error.value.status_code in (403, 404)
        with pytest.raises(HTTPException) as error:
            asyncio.run(get_edit_media("recipe-test", "recipes-json", {"id": "owner"}))
        assert error.value.status_code == 404
        save_recipe_files(tmp_path, summarize_recipes(Agent([[]]), rows(1)))
        response = asyncio.run(get_edit_media("recipe-test", "recipes-zip", {"id": "owner"}))
        assert response.filename == "recipes.zip"
    finally:
        LIVE_EDIT_JOBS.pop("recipe-test", None)

import json

import pytest

from app.services.recipe_summary import summarize_recipes
from app.services.recipe_menu_plan import validate_menu_plan
from app.services.recipe_thumbnail import describe_recipe_thumbnail
from app.services.llm_analysis_service import LLMAnalysisError
from app.services.llm_gateway import LLMGateway, LLMGatewayError


def menu(name, ids, components=None):
    return {"name": name, "components": components or [], "source_ids": ids, "reason": "독립된 음식 조리"}


def test_two_dishes_are_fixed_before_extracting_sauce_steps():
    calls = []
    fixed = [menu("떡볶이", [0], ["고추장"]), menu("김밥", [2])]

    class Agent:
        def _request_json(self, system, prompt, **kwargs):
            data = json.loads(prompt)
            calls.append(data)
            if data.get("task") in {"menu_scan", "menu_plan"}:
                return kwargs["validator"]({"menus": fixed, "uncertainties": []})
            assert data["known_menus"] == ["떡볶이", "김밥"]
            assert data["main_menu_plan"]["menu_count"] == 2
            assert kwargs["response_schema"]["properties"]["recipes"]["items"]["properties"]["menu_name"]["enum"] == ["떡볶이", "김밥"]
            return kwargs["validator"]({"recipes": [
                {"menu_name": "떡볶이", "steps": [{"stage": "조리", "instruction": "고추장을 넣는다", "source_ids": [1]}], "uncertainties": []},
                {"menu_name": "김밥", "steps": [{"stage": "조리", "instruction": "김밥을 만다", "source_ids": [2]}], "uncertainties": []},
            ]})

    rows = [{"id": i, "text": text, "start": i * 5, "end": i * 5 + 5} for i, text in enumerate(
        ["떡볶이와 김밥을 만듭니다", "고추장을 넣습니다", "김밥을 말아 주세요"])]
    result = summarize_recipes(Agent(), rows, video_context={"title": "떡볶이와 김밥 두 가지"})
    assert [c.get("task") for c in calls] == ["menu_scan", "menu_plan", None]
    assert calls[1]["video_context"]["title"] == "떡볶이와 김밥 두 가지"
    assert result["main_menu_plan"]["menu_count"] == 2
    assert [r["menu_name"] for r in result["recipes"]] == ["떡볶이", "김밥"]


def test_component_cannot_be_invented_as_a_third_recipe():
    class Agent:
        def _request_json(self, system, prompt, **kwargs):
            if json.loads(prompt).get("task"):
                return kwargs["validator"]({"menus": [menu("떡볶이", [0], ["고추장"])], "uncertainties": []})
            return kwargs["validator"]({"recipes": [{"menu_name": "고추장", "steps": [
                {"stage": "재료 설명", "instruction": "고추장 한 숟갈", "source_ids": [0]}
            ], "uncertainties": []}]})
    with pytest.raises(LLMAnalysisError, match="확정된 주 요리"):
        summarize_recipes(Agent(), [{"id": 0, "text": "떡볶이에 고추장 한 숟갈", "start": 0, "end": 4}])


def test_menu_names_are_not_hardcoded_to_exclude_sauce_making_videos():
    result = validate_menu_plan({"menus": [menu("수제 고추장", [0])], "uncertainties": []}, {0})
    assert result["menus"][0]["name"] == "수제 고추장"


@pytest.mark.parametrize("menus", [[menu("국", [99])], [menu("국", [0]), menu("국", [0])]])
def test_invalid_global_menu_plan_is_rejected(menus):
    with pytest.raises(LLMAnalysisError):
        validate_menu_plan({"menus": menus, "uncertainties": []}, {0})


def test_missing_thumbnail_does_not_claim_visual_analysis(tmp_path):
    assert describe_recipe_thumbnail(object(), tmp_path)["status"] == "unavailable"


def test_thumbnail_is_sent_as_image_and_failure_is_explicit(tmp_path):
    (tmp_path / "sddefault.jpg").write_bytes(b"\xff\xd8\xfftest")

    class Agent:
        class gateway:
            provider = "gemini"

        def _request_json(self, *args, **kwargs):
            assert kwargs["image"]["mime_type"] == "image/jpeg"
            assert kwargs["image"]["data"]
            return kwargs["validator"]({"observations": ["두 접시"], "uncertainties": ["음식 이름 불확실"]})

    assert describe_recipe_thumbnail(Agent(), tmp_path)["status"] == "analyzed"

    class Failed(Agent):
        def _request_json(self, *args, **kwargs):
            raise LLMAnalysisError("원격 서버 재배포 필요")

    assert describe_recipe_thumbnail(Failed(), tmp_path) == {"status": "unavailable", "reason": "원격 서버 재배포 필요"}


def test_old_remote_server_cannot_silently_ignore_thumbnail(monkeypatch):
    monkeypatch.setenv("AVE_SERVER_URL", "https://example.test")

    class Response:
        status_code = 200
        def raise_for_status(self):
            pass
        def json(self):
            return {"text": "{}"}

    monkeypatch.setattr("app.services.llm_gateway.requests.post", lambda *a, **k: Response())
    with pytest.raises(LLMGatewayError, match="서버 재배포"):
        LLMGateway("gemini", server_access_token="test").request_json("role", "prompt", image={"mime_type": "image/jpeg", "data": "test"})

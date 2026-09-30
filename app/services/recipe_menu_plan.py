"""전체 흐름에서 주 요리를 확정한 뒤 레시피 추출에 고정 목록을 제공한다."""
import json

from app.services.llm_analysis_service import LLMAnalysisError
from app.services.prompt_store import system_prompt


def menu_schema(ids):
    return {"type": "object", "additionalProperties": False, "required": ["menus", "uncertainties"],
            "properties": {
                "menus": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["name", "components", "source_ids", "reason"],
                    "properties": {
                        "name": {"type": "string", "minLength": 1},
                        "components": {"type": "array", "items": {"type": "string"}},
                        "source_ids": {"type": "array", "minItems": 1,
                                       "items": {"type": "integer", "minimum": min(ids), "maximum": max(ids)}},
                        "reason": {"type": "string", "minLength": 1},
                    }}},
                "uncertainties": {"type": "array", "items": {"type": "string"}},
            }}


def validate_menu_plan(raw, ids):
    if not isinstance(raw, dict) or set(raw) != {"menus", "uncertainties"}:
        raise LLMAnalysisError("메뉴 판별 응답은 menus와 uncertainties만 포함해야 합니다.")
    if not isinstance(raw["menus"], list) or not isinstance(raw["uncertainties"], list) or any(
        not isinstance(x, str) for x in raw["uncertainties"]
    ):
        raise LLMAnalysisError("menus는 배열, uncertainties는 문자열 배열이어야 합니다.")
    names = set()
    for index, menu in enumerate(raw["menus"]):
        if not isinstance(menu, dict) or set(menu) != {"name", "components", "source_ids", "reason"}:
            raise LLMAnalysisError(f"menus[{index}]: name, components, source_ids, reason이 필요합니다.")
        if any(not isinstance(menu[k], str) or not menu[k].strip() for k in ("name", "reason")):
            raise LLMAnalysisError(f"menus[{index}]: 메뉴명과 주 요리로 판단한 근거를 작성하세요.")
        if not isinstance(menu["components"], list) or any(not isinstance(x, str) for x in menu["components"]):
            raise LLMAnalysisError(f"menus[{index}].components는 문자열 배열이어야 합니다.")
        refs = menu["source_ids"]
        if not isinstance(refs, list) or not refs or any(type(i) is not int or i not in ids for i in refs):
            raise LLMAnalysisError(f"menus[{index}].source_ids에는 이번 입력의 실제 정수 ID가 필요합니다.")
        name = menu["name"].strip()
        if name in names:
            raise LLMAnalysisError("같은 주 요리를 중복 메뉴로 반환하지 마세요.")
        names.add(name)
    return raw


def identify_main_menus(agent, batches, *, context=None, cancel_callback=None, progress_callback=None):
    context = context or {}
    evidence = []
    for index, batch in enumerate(batches, 1):
        ids = {row["id"] for row in batch}
        scan = agent._request_json(
            system_prompt("recipe_menu_scan"),
            json.dumps({"task": "menu_scan", "video_context": context, "segments": batch}, ensure_ascii=False),
            response_schema=menu_schema(ids), validator=lambda raw: validate_menu_plan(raw, ids),
            cancel_callback=cancel_callback,
        )
        evidence.append({"batch": index, **scan})
        if progress_callback:
            progress_callback(index, len(batches))
    if not evidence:
        return {"menus": [], "uncertainties": [], "menu_count": 0}
    # 후보를 통합할 때는 중간 요약에서 실제로 인용된 ID만 허용한다.
    ids = {i for scan in evidence for menu in scan["menus"] for i in menu["source_ids"]}
    if not ids:
        return {"menus": [], "uncertainties": list(dict.fromkeys(
            warning for scan in evidence for warning in scan["uncertainties"]
        )), "menu_count": 0}
    plan = agent._request_json(
        system_prompt("recipe_menu_plan"),
        json.dumps({"task": "menu_plan", "video_context": context, "whole_video_evidence": evidence}, ensure_ascii=False),
        response_schema=menu_schema(ids), validator=lambda raw: validate_menu_plan(raw, ids),
        cancel_callback=cancel_callback,
    )
    plan = {**plan, "menus": sorted(plan["menus"], key=lambda m: min(m["source_ids"]))}
    plan["menu_count"] = len(plan["menus"])
    return plan

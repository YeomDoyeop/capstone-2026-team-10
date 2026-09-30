"""원본 자막 근거를 보존하는 메뉴별 레시피 요약 및 다운로드 파일."""
import json
import logging
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

from app.services.llm_analysis_service import LLMAnalysisError
from app.services.prompt_store import system_prompt
from app.services.recipe_menu_plan import identify_main_menus

STAGES = ("재료 설명", "재료 손질", "조리", "완성")


def recipe_schema(ids):
    def obj(properties):
        return {"type": "object", "properties": properties,
                "required": list(properties), "additionalProperties": False}
    return obj({"recipes": {"type": "array", "items": obj({
        "menu_name": {"type": "string", "minLength": 1},
        "steps": {"type": "array", "minItems": 1, "items": obj({
            "stage": {"type": "string", "enum": list(STAGES)},
            "instruction": {"type": "string", "minLength": 1},
            "source_ids": {"type": "array", "minItems": 1,
                           "items": {"type": "integer", "enum": ids}},
        })},
        "uncertainties": {"type": "array", "items": {"type": "string"}},
    })}})


def validate_recipe_response(raw, allowed):
    """내용·근거는 엄격히 검증하고, 확인 사항만 정보 손실 없이 정규화한다."""
    def fail(path, message):
        raise LLMAnalysisError(f"{path}: {message}")

    if not isinstance(raw, dict) or set(raw) != {"recipes"}:
        fail("recipes", '최상위 객체에는 recipes 키만 허용됩니다. 요리 내용이 없으면 {"recipes":[]}를 반환하세요.')
    if not isinstance(raw["recipes"], list):
        fail("recipes", f"배열이 필요합니다. 받은 타입={type(raw['recipes']).__name__}.")
    result = []
    for index, recipe in enumerate(raw["recipes"]):
        path = f"recipes[{index}]"
        if not isinstance(recipe, dict):
            fail(path, f"menu_name, steps, uncertainties를 가진 객체가 필요합니다. 받은 타입={type(recipe).__name__}.")
        missing = {"menu_name", "steps"} - set(recipe)
        if missing:
            fail(path, f"필수 필드 누락: {', '.join(sorted(missing))}. 빈 메뉴나 조리 내용을 만들어 채우지 마세요.")
        if set(recipe) - {"menu_name", "steps", "uncertainties"}:
            fail(path, "추가 필드는 금지됩니다. menu_name, steps, uncertainties만 반환하고 재료 정보는 steps의 '재료 설명'으로 작성하세요.")
        if not isinstance(recipe["menu_name"], str) or not recipe["menu_name"].strip():
            fail(path + ".menu_name", "공백이 아닌 메뉴 이름 문자열이 필요합니다. 이름을 추측하지 말고 확인 불가라면 '이름 미확인 메뉴'와 uncertainties를 사용하세요.")
        if not isinstance(recipe["steps"], list):
            fail(path + ".steps", f"단계 객체의 배열이 필요합니다. 받은 타입={type(recipe['steps']).__name__}.")
        if not recipe["steps"]:
            fail(path + ".steps", '단계가 빈 메뉴는 허용되지 않습니다. 실제 근거가 있는 단계만 작성하고 이번 구간에 요리 내용이 없으면 {"recipes":[]}를 반환하세요.')
        warnings = recipe.get("uncertainties")
        if warnings is None:
            # 불확실성이 없다고 단정하지 않고 누락 사실을 결과에 보존한다.
            warnings = ["모델이 확인 필요 사항을 제공하지 않았습니다. 원본 확인이 필요합니다."]
        elif isinstance(warnings, str):
            warnings = [warnings] if warnings.strip() else []
        if not isinstance(warnings, list) or any(not isinstance(x, str) for x in warnings):
            fail(path + ".uncertainties", "문자열 배열이 필요합니다. 확인 사항이 없으면 []를 사용하세요.")
        steps = []
        for step_index, step in enumerate(recipe["steps"]):
            step_path = f"{path}.steps[{step_index}]"
            if not isinstance(step, dict) or set(step) != {"stage", "instruction", "source_ids"}:
                fail(step_path, "stage, instruction, source_ids만 가진 객체가 필요합니다. 단계 번호·시간은 코드가 생성하므로 추가하지 마세요.")
            if step["stage"] not in STAGES:
                fail(step_path + ".stage", "허용 값은 '재료 설명', '재료 손질', '조리', '완성'입니다.")
            if not isinstance(step["instruction"], str) or not step["instruction"].strip():
                fail(step_path + ".instruction", "공백이 아닌 조리 설명 문자열이 필요합니다.")
            refs = step["source_ids"]
            if not isinstance(refs, list) or not refs:
                fail(step_path + ".source_ids", "근거 자막 ID를 한 개 이상 담은 정수 배열이 필요합니다.")
            if any(type(i) is not int or i not in allowed for i in refs):
                fail(step_path + ".source_ids", f"이번 입력의 정수 ID만 사용하세요. 허용 범위={min(allowed)}~{max(allowed)}; 문자열·시간값·입력 밖 ID는 금지됩니다.")
            steps.append({**step, "source_ids": list(refs)})
        result.append({"menu_name": recipe["menu_name"].strip(), "steps": steps, "uncertainties": list(warnings)})
    return result


def summarize_recipes(agent, segments, *, video_context=None, cancel_callback=None, progress_callback=None):
    by_id = {int(row["id"]): row for row in segments}
    batches, batch, size = [], [], 0
    for row in segments:
        item = {"id": int(row["id"]), "text": str(row["text"])}
        count = len(json.dumps(item, ensure_ascii=False))
        if batch and (size + count > 8000 or len(batch) >= 80):
            batches.append(batch)
            batch, size = [], 0
        batch.append(item)
        size += count
    if batch:
        batches.append(batch)
    plan = identify_main_menus(agent, batches, context=video_context,
                               cancel_callback=cancel_callback, progress_callback=progress_callback)
    fixed_names = [menu["name"].strip() for menu in plan["menus"]]
    menus = {}
    for number, batch in enumerate(batches if fixed_names else [], 1):
        allowed = {row["id"] for row in batch}

        def validate(raw):
            try:
                validated = validate_recipe_response(raw, allowed)
                if any(recipe["menu_name"] not in fixed_names for recipe in validated):
                    raise LLMAnalysisError("menu_name은 확정된 주 요리 이름만 허용합니다. 부재료를 새 메뉴로 만들지 말고 main_menu_plan의 주 요리에 배정하세요.")
                return validated
            except LLMAnalysisError as exc:
                detail = f"레시피 묶음 {number}/{len(batches)}: {exc}"
                # 원문 응답·자막·키 대신 오류 위치와 기대 형식만 기록한다.
                logging.getLogger(__name__).warning("%s", detail)
                raise LLMAnalysisError(detail) from exc

        schema = recipe_schema(sorted(allowed))
        schema["properties"]["recipes"]["items"]["properties"]["menu_name"]["enum"] = fixed_names
        recipes = agent._request_json(
            system_prompt("recipe_summary"),
            json.dumps({"known_menus": fixed_names, "main_menu_plan": plan, "video_context": video_context or {}, "segments": batch}, ensure_ascii=False),
            response_schema=schema, validator=validate, cancel_callback=cancel_callback,
        )
        for recipe in recipes:
            name = recipe["menu_name"].strip()
            menu = menus.setdefault(name, {"menu_name": name, "steps": [], "uncertainties": []})
            menu["uncertainties"] = list(dict.fromkeys(menu["uncertainties"] + recipe["uncertainties"]))
            for step in recipe["steps"]:
                refs = sorted(set(step["source_ids"]))
                entry = {**step, "source_ids": refs, "source_ranges": [
                    {"segment_id": i, "start": float(by_id[i]["start"]), "end": float(by_id[i]["end"])}
                    for i in refs
                ]}
                if not any(old["instruction"] == entry["instruction"] and old["source_ids"] == refs for old in menu["steps"]):
                    menu["steps"].append(entry)
        if progress_callback:
            progress_callback(number, len(batches))
    result = sorted(menus.values(), key=lambda menu: min(
        i for step in menu["steps"] for i in step["source_ids"]
    ))
    for menu_index, menu in enumerate(result, 1):
        menu["recipe_index"] = menu_index
        for step_index, step in enumerate(menu["steps"], 1):
            step["step_index"] = step_index
    for name in fixed_names:
        if name not in menus:
            plan["uncertainties"].append(f"{name}: 근거 있는 상세 조리 단계를 추출하지 못했습니다.")
    return {"scope": "original_transcript", "time_basis": "source_video_seconds", "main_menu_plan": plan,
            "video_context": video_context or {},
            "note": "원본 자막 기반 요약입니다. 편집본에서 제외된 단계도 포함합니다. 자막 오인식과 조리 순서는 원본 영상으로 확인하세요.",
            "recipes": result}


def recipe_markdown(document):
    lines = ["# 메뉴별 레시피 요약", "", document["note"], ""]
    if "main_menu_plan" in document:
        lines += [f"판별한 주 요리: {document['main_menu_plan']['menu_count']}개", ""]
        lines += [f"- 확인 필요: {warning}" for warning in document["main_menu_plan"]["uncertainties"]]
    thumbnail = document.get("video_context", {}).get("thumbnail", {})
    if thumbnail.get("status") == "unavailable":
        lines += ["", "썸네일 미분석: " + thumbnail.get("reason", "이미지 분석 불가"), ""]
    if not document["recipes"]:
        lines += ["원본 자막에서 확인 가능한 레시피를 찾지 못했습니다.", ""]
    for menu in document["recipes"]:
        lines += [f"## {menu['recipe_index']:02d}. {menu['menu_name']}", ""]
        for step in menu["steps"]:
            refs = ", ".join(f"{r['start']:.2f}~{r['end']:.2f}초" for r in step["source_ranges"])
            lines += [f"{step['step_index']}. [{step['stage']}] {step['instruction']}",
                      f"   - 원본 위치: {refs}", ""]
        for warning in menu["uncertainties"]:
            lines += [f"- 확인 필요: {warning}"]
        lines.append("")
    return "\n".join(lines)


def save_recipe_files(output_dir: Path, document):
    """고정 파일명만 사용한다. LLM 메뉴 이름은 경로에 사용하지 않는다."""
    output_dir.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(document, ensure_ascii=False, indent=2)
    markdown = recipe_markdown(document)
    (output_dir / "recipes.json").write_text(encoded, encoding="utf-8")
    (output_dir / "recipes.md").write_text(markdown, encoding="utf-8")
    with ZipFile(output_dir / "recipes.zip", "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("recipes.json", encoded)
        archive.writestr("recipes.md", markdown)
        for menu in document["recipes"]:
            prefix = f"{menu['recipe_index']:02d}-recipe"
            archive.writestr(prefix + ".json", json.dumps(menu, ensure_ascii=False, indent=2))
            archive.writestr(prefix + ".md", recipe_markdown({**document, "recipes": [menu]}))

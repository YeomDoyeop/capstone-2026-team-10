"""로컬 JSON 프롬프트 파일 조회와 사용자 판별 기준 관리."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


class PromptStoreError(RuntimeError):
    pass


PROMPT_ROOT = Path(__file__).resolve().parents[2] / "prompts"
USER_PROMPT_DIR = PROMPT_ROOT / "user"
SYSTEM_PROMPT_DIR = PROMPT_ROOT / "system"
SCHEMA_DIR = PROMPT_ROOT / "schemas"
PROMPT_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PromptStoreError(f"프롬프트 파일을 읽을 수 없습니다: {path.name}") from exc
    if not isinstance(value, dict):
        raise PromptStoreError(f"프롬프트 파일은 JSON 객체여야 합니다: {path.name}")
    return value


def system_prompt(prompt_id: str) -> str:
    value = _read_object(SYSTEM_PROMPT_DIR / f"{prompt_id}.json").get("prompt")
    if not isinstance(value, str) or not value.strip():
        raise PromptStoreError(f"시스템 프롬프트가 비어 있습니다: {prompt_id}")
    return value.strip()


def response_schema(schema_id: str) -> dict[str, Any]:
    if not PROMPT_ID.fullmatch(schema_id):
        raise PromptStoreError("잘못된 응답 스키마 ID입니다.")
    return _read_object(SCHEMA_DIR / f"{schema_id}.json")


def list_user_prompts() -> list[dict[str, Any]]:
    result = []
    for path in sorted(USER_PROMPT_DIR.glob("*.json")):
        value = _read_object(path)
        if not PROMPT_ID.fullmatch(path.stem):
            continue
        result.append({"id": path.stem, **value})
    return result


def user_prompt(prompt_id: str) -> dict[str, Any]:
    if not PROMPT_ID.fullmatch(prompt_id):
        raise PromptStoreError("잘못된 판별 기준 ID입니다.")
    path = USER_PROMPT_DIR / f"{prompt_id}.json"
    if not path.is_file():
        raise PromptStoreError("판별 기준을 찾을 수 없습니다.")
    return {"id": prompt_id, **_read_object(path)}


def save_user_prompt(prompt_id: str, value: dict[str, Any], *, create: bool) -> dict[str, Any]:
    if not PROMPT_ID.fullmatch(prompt_id):
        raise PromptStoreError("ID는 영문 소문자, 숫자, 하이픈, 밑줄만 사용할 수 있습니다.")
    cleaned = {}
    for key, item in value.items():
        if key == "id":
            continue
        if not isinstance(key, str) or not key.strip() or not isinstance(item, str):
            raise PromptStoreError("판별 기준의 모든 키와 값은 문자열이어야 합니다.")
        cleaned[key.strip()] = item
    if not cleaned.get("name", "").strip() or not cleaned.get("criteria", "").strip():
        raise PromptStoreError("name과 criteria는 필수입니다.")
    USER_PROMPT_DIR.mkdir(parents=True, exist_ok=True)
    path = USER_PROMPT_DIR / f"{prompt_id}.json"
    if create and path.exists():
        raise PromptStoreError("같은 ID의 판별 기준이 이미 있습니다.")
    path.write_text(json.dumps(cleaned, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"id": prompt_id, **cleaned}


def delete_user_prompt(prompt_id: str) -> None:
    if not PROMPT_ID.fullmatch(prompt_id):
        raise PromptStoreError("잘못된 판별 기준 ID입니다.")
    path = USER_PROMPT_DIR / f"{prompt_id}.json"
    if not path.is_file():
        raise PromptStoreError("판별 기준을 찾을 수 없습니다.")
    path.unlink()

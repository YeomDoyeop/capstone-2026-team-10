import json

import pytest

from app.services import prompt_store


def test_user_prompt_crud_uses_json_files(tmp_path, monkeypatch):
    user_dir = tmp_path / "prompts" / "user"
    user_dir.mkdir(parents=True)
    monkeypatch.setattr(prompt_store, "USER_PROMPT_DIR", user_dir)
    created = prompt_store.save_user_prompt(
        "custom_news",
        {"name": "사용자 뉴스", "description": "설명", "criteria": "핵심 수치"},
        create=True,
    )
    assert created["id"] == "custom_news"
    assert prompt_store.user_prompt("custom_news")["criteria"] == "핵심 수치"
    assert json.loads((user_dir / "custom_news.json").read_text(encoding="utf-8"))["name"] == "사용자 뉴스"
    assert prompt_store.list_user_prompts()[0]["id"] == "custom_news"
    prompt_store.delete_user_prompt("custom_news")
    assert not (user_dir / "custom_news.json").exists()


def test_user_prompt_rejects_unsafe_id_and_missing_contract(tmp_path, monkeypatch):
    monkeypatch.setattr(prompt_store, "USER_PROMPT_DIR", tmp_path)
    with pytest.raises(prompt_store.PromptStoreError):
        prompt_store.save_user_prompt("../outside", {"name": "x", "criteria": "y"}, create=True)
    with pytest.raises(prompt_store.PromptStoreError):
        prompt_store.save_user_prompt("missing", {"name": "x"}, create=True)


def test_system_prompts_are_read_from_json_files(tmp_path, monkeypatch):
    system_dir = tmp_path / "system"
    system_dir.mkdir()
    (system_dir / "chapter.json").write_text(
        json.dumps({"prompt": "파일에서 읽은 챕터 프롬프트"}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(prompt_store, "SYSTEM_PROMPT_DIR", system_dir)
    assert prompt_store.system_prompt("chapter") == "파일에서 읽은 챕터 프롬프트"


def test_response_schema_is_read_from_json_file(tmp_path, monkeypatch):
    schema_dir = tmp_path / "schemas"
    schema_dir.mkdir()
    (schema_dir / "score.json").write_text(
        json.dumps({"type": "object", "required": ["items"]}), encoding="utf-8"
    )
    monkeypatch.setattr(prompt_store, "SCHEMA_DIR", schema_dir)
    assert prompt_store.response_schema("score")["required"] == ["items"]


def test_bundled_streaming_profiles_cover_major_content_types():
    profiles = {item["id"]: item for item in prompt_store.list_user_prompts()}
    expected = {
        "ai_news",
        "stock",
        "game",
        "news_current_affairs",
        "sports",
        "music_performance",
        "education_course",
        "podcast_interview",
        "shopping_product",
        "fashion_beauty",
        "film_entertainment",
        "vlog_irl",
        "conference_event",
        "science_technology",
        "cooking_food",
        "creative_art",
        "asmr_ambient",
        "religion_spirituality",
        "community_talk",
        "health_fitness",
        "pets_animals",
        "automotive_mobility",
        "business_webinar",
        "general_stream",
    }
    assert expected <= profiles.keys()
    assert all(len(profiles[prompt_id]["criteria"]) >= 100 for prompt_id in expected)


def test_section_score_prompt_requires_relative_calibration():
    value = prompt_store.system_prompt("section_score")
    assert "반사실적으로" in value
    assert "상대 비교" in value
    assert "900점 이상" in value

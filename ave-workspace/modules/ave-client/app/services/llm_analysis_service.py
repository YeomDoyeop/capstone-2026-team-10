"""공급자와 무관한 JSON 기반 영상 구조화·하이라이트 분석 서비스."""

from __future__ import annotations
import json
import hashlib
import logging
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable
from app.services.edit_policy import SHORT_FORM_PROFILES
from app.services.llm_gateway import LLMGateway, LLMGatewayError, safe_error_detail
from app.services.prompt_store import (
    PromptStoreError,
    response_schema,
    system_prompt,
    user_prompt,
)


class LLMAnalysisError(RuntimeError):
    """LLM 응답이 분석 계약을 지키지 않았을 때 발생한다."""


JSON_MAX_ATTEMPTS = 3
TRANSPORT_MAX_ATTEMPTS = 4
MAX_AUTOMATIC_WAIT_SECONDS = 60
DEFAULT_PARALLEL_REQUESTS = 20


def _parse_json_object(text: str) -> dict[str, Any]:
    # 완전한 코드펜스 하나만 허용한다. 잘린 JSON을 추측해서 복구하지 않는다.
    value = text.strip()
    if value.startswith("```"):
        match = re.fullmatch(r"```(?:json)?\s*\n(.*?)\n```", value, re.DOTALL | re.IGNORECASE)
        if match:
            value = match.group(1)

    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise LLMAnalysisError("JSON 객체 키가 중복되었습니다.")
            result[key] = item
        return result

    def invalid_constant(_value):
        raise LLMAnalysisError("JSON에 NaN 또는 Infinity를 사용할 수 없습니다.")

    parsed = json.loads(value, object_pairs_hook=pairs, parse_constant=invalid_constant)
    if not isinstance(parsed, dict):
        raise LLMAnalysisError("응답 최상위는 JSON 객체여야 합니다.")
    return parsed


WHISPER_ENTITY_TYPES = frozenset(
    response_schema("whisper_settings")["properties"]["hotwords"]["items"]["properties"]["entity_type"]["enum"]
)


class LLMAnalysisService:
    def __init__(
        self,
        *,
        provider: str = "deepseek",
        server_access_token: str | None = None,
        checkpoint: dict[str, Any] | None = None,
        checkpoint_callback: Callable[[], None] | None = None,
        **_: Any,
    ):
        try:
            self.gateway = LLMGateway(provider, server_access_token=server_access_token)
        except LLMGatewayError as exc:
            raise LLMAnalysisError(str(exc)) from exc
        self._max_parallel_requests = self.gateway.max_parallel_requests
        self._minimum_request_interval_seconds = (
            self.gateway.minimum_request_interval_seconds
        )
        self._request_limit_lock = threading.Lock()
        self._last_request_started_at = 0.0
        self._checkpoint = checkpoint if checkpoint is not None else {}
        self._checkpoint_responses = self._checkpoint.setdefault("llm_responses", {})
        self._checkpoint_callback = checkpoint_callback
        self._checkpoint_lock = threading.Lock()

    def _wait_for_request_slot(
        self, cancel_callback: Callable[[], None] | None
    ) -> None:
        interval = getattr(self, "_minimum_request_interval_seconds", 0.0)
        if interval <= 0:
            return
        with self._request_limit_lock:
            remaining = interval - (time.monotonic() - self._last_request_started_at)
            while remaining > 0:
                if cancel_callback:
                    cancel_callback()
                time.sleep(min(0.25, remaining))
                remaining = interval - (
                    time.monotonic() - self._last_request_started_at
                )
            if cancel_callback:
                cancel_callback()
            self._last_request_started_at = time.monotonic()

    def _retry_pause(self, seconds, cancel_callback):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if cancel_callback:
                cancel_callback()
            time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))

    def _request_json(
        self,
        system: str,
        prompt: str,
        *,
        response_schema: dict[str, Any] | None = None,
        image: dict[str, str] | None = None,
        validator: Callable[[Any], Any] | None = None,
        cancel_callback: Callable[[], None] | None = None,
    ) -> Any:
        system = (
            "[공통 응답 계약]\n" + system_prompt("json_contract")
            + "\n\n[작업 지침]\n" + system
            + "\n\n[출력 스키마]\n"
            + json.dumps(response_schema or {"type": "object"}, ensure_ascii=False, separators=(",", ":"))
        )
        cache_key = hashlib.sha256(
            json.dumps(
                [getattr(self.gateway, "provider", ""), getattr(self.gateway, "model", ""),
                 system, prompt, response_schema] + ([image] if image is not None else []),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        checkpoint_lock = getattr(self, "_checkpoint_lock", None)
        responses = getattr(self, "_checkpoint_responses", {})
        if checkpoint_lock is None:
            cached = None
        else:
            with checkpoint_lock:
                cached = responses.get(cache_key)
        if cached is not None:
            try:
                value = validator(cached) if validator else cached
            except LLMAnalysisError:
                with checkpoint_lock:
                    responses.pop(cache_key, None)
            else:
                if cancel_callback:
                    cancel_callback()
                return value
        last_error: Exception | None = None
        invalid_count = 0
        transport_count = 0
        while invalid_count < JSON_MAX_ATTEMPTS:
            if cancel_callback:
                cancel_callback()
            self._wait_for_request_slot(cancel_callback)
            rule = (
                ""
                if last_error is None
                else (
                    "\n직전 응답 거부 사유: "
                    + safe_error_detail(last_error)
                    + " 설명하지 말고 이 사유를 고쳐 완결된 JSON 객체 하나만 반환하세요."
                )
            )
            try:
                raw = _parse_json_object(
                    self.gateway.request_json(
                        system + rule, prompt, response_schema=response_schema,
                        **({"image": image} if image is not None else {}),
                    )
                )
                value = validator(raw) if validator else raw
            except LLMGatewayError as exc:
                transport_count += 1
                if not exc.retryable or transport_count >= TRANSPORT_MAX_ATTEMPTS:
                    raise LLMAnalysisError(
                        f"LLM 통신/API 오류 (호출 실패 {transport_count}회): {exc}"
                    ) from exc
                delay = max(2 ** transport_count + random.uniform(0, 1), exc.retry_after or 0)
                if delay > MAX_AUTOMATIC_WAIT_SECONDS:
                    raise LLMAnalysisError(
                        f"LLM 서버가 {delay:.0f}초 후 재요청을 요구했습니다. 기다린 뒤 재시도하세요: {exc}"
                    ) from exc
                logging.getLogger(__name__).warning(
                    "LLM 일시 오류, %.1f초 후 재시도 (%d/%d): %s",
                    delay, transport_count, TRANSPORT_MAX_ATTEMPTS, safe_error_detail(exc),
                )
                self._retry_pause(delay, cancel_callback)
                continue
            except (json.JSONDecodeError, LLMAnalysisError) as exc:
                last_error = exc
                invalid_count += 1
                continue
            if cancel_callback:
                cancel_callback()
            if checkpoint_lock is not None:
                with checkpoint_lock:
                    responses[cache_key] = raw
                    callback = getattr(self, "_checkpoint_callback", None)
                    if callback:
                        callback()
            return value
        detail = f": {last_error}" if last_error is not None else ""
        raise LLMAnalysisError(
            f"LLM JSON/응답 계약 검증이 {JSON_MAX_ATTEMPTS}회 실패했습니다{detail}"
        ) from last_error

    @staticmethod
    def _validated_whisper_settings(
        raw: Any, *, metadata_text: str = ""
    ) -> dict[str, Any]:
        if not isinstance(raw, dict) or set(raw) != {"hotwords"}:
            raise LLMAnalysisError("Whisper 설정 응답 객체 형식이 올바르지 않습니다.")
        hotwords = raw.get("hotwords")
        if not isinstance(hotwords, list) or len(hotwords) > 10:
            raise LLMAnalysisError("Whisper 설정 응답 필드 형식이 올바르지 않습니다.")
        cleaned: list[str] = []
        seen: set[str] = set()
        for item in hotwords:
            if not isinstance(item, dict) or set(item) != {"text", "entity_type"}:
                raise LLMAnalysisError(
                    "Whisper 핫워드는 고유명사와 개체 유형을 포함해야 합니다."
                )
            value, entity_type = item.get("text"), item.get("entity_type")
            if not isinstance(value, str) or entity_type not in WHISPER_ENTITY_TYPES:
                raise LLMAnalysisError(
                    "Whisper 핫워드의 고유명사 유형이 올바르지 않습니다."
                )
            word = value.strip()
            if not word or len(word) > 40:
                raise LLMAnalysisError("Whisper 핫워드 길이가 올바르지 않습니다.")
            if metadata_text and word.casefold() not in metadata_text.casefold():
                raise LLMAnalysisError(
                    "Whisper 핫워드는 입력 메타데이터의 원문 표기여야 합니다."
                )
            key = word.casefold()
            if key not in seen:
                seen.add(key)
                cleaned.append(word)
        return {"hotwords": ", ".join(cleaned)}

    def recommend_whisper_settings(self, metadata: dict[str, Any]) -> dict[str, Any]:
        prompt = json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
        return self._request_json(
            system_prompt("whisper_settings"),
            prompt,
            response_schema=response_schema("whisper_settings"),
            validator=lambda raw: self._validated_whisper_settings(
                raw, metadata_text=prompt
            ),
        )

    def split_subtitle_words(
        self,
        words: list[dict[str, Any]],
        *,
        cancel_callback: Callable[[], None] | None = None,
    ) -> list[dict[str, int]]:
        rows = [
            {"id": index, "text": str(word["word"])} for index, word in enumerate(words)
        ]
        if len(rows) < 2:
            raise LLMAnalysisError("자막을 분할할 단어가 부족합니다.")

        def validate(raw: Any) -> list[dict[str, int]]:
            if not isinstance(raw, dict) or set(raw) != {"indexes"}:
                raise LLMAnalysisError("자막 분할 응답 객체 형식이 올바르지 않습니다.")
            indexes = raw.get("indexes")
            if (
                not isinstance(indexes, list)
                or any(type(index) is not int for index in indexes)
                or indexes != sorted(set(indexes))
                or any(index < 0 or index >= len(rows) for index in indexes)
            ):
                raise LLMAnalysisError("자막 분할 경계 인덱스가 올바르지 않습니다.")
            # 일부 모델은 마지막 자막 조각의 종료 단어도 경계로 덧붙인다.
            # 마지막 단어 ID는 빈 조각을 만들지 않는 종료 표식이므로 이 값
            # 하나만 제거한다. 다른 범위·중복·순서 위반은 그대로 거부한다.
            if indexes and indexes[-1] == len(rows) - 1:
                indexes = indexes[:-1]
            starts = [0, *(index + 1 for index in indexes)]
            ends = [*indexes, len(rows) - 1]
            return [
                {"start_word": start, "end_word": end}
                for start, end in zip(starts, ends)
            ]

        prompt = json.dumps({"words": rows}, ensure_ascii=False, separators=(",", ":"))
        return self._request_json(
            system_prompt("subtitle_split"),
            prompt,
            response_schema=response_schema("subtitle_split"),
            validator=validate,
            cancel_callback=cancel_callback,
        )

    @staticmethod
    def _validated_ranges(
        raw: Any, ids: list[int], *, key: str, require_chapter_fields: bool
    ) -> list[dict[str, Any]]:
        if not isinstance(raw, dict) or set(raw) != {key}:
            raise LLMAnalysisError(f"{key} 응답 객체 형식이 올바르지 않습니다.")
        values = raw.get(key)
        if not isinstance(values, list) or not values:
            raise LLMAnalysisError(f"{key} 응답 형식이 올바르지 않습니다.")
        expected = ids[0]
        known = set(ids)
        result = []
        for index, item in enumerate(values):
            if not isinstance(item, dict):
                raise LLMAnalysisError(f"{key} 항목 형식이 올바르지 않습니다.")
            expected_fields = (
                {"start_id", "end_id", "summary", "score"}
                if require_chapter_fields
                else {"start_id", "end_id"}
            )
            if set(item) != expected_fields:
                raise LLMAnalysisError(f"{key} 항목 필드가 응답 계약과 다릅니다.")
            start, end = item.get("start_id"), item.get("end_id")
            # 응답 본문이나 임의 문자열을 오류에 노출하지 않고 ID만 진단한다.
            def describe_id(value: Any) -> str:
                if value is None or type(value) in (bool, int, float):
                    return f"{str(value)[:32]} ({type(value).__name__})"
                if isinstance(value, str) and re.fullmatch(r"-?\d{1,16}(?:\.\d{1,8})?", value):
                    return f"{value!r} (str)"
                return f"<{type(value).__name__}>"

            detail = (
                f"{key}[{index}]: start_id={describe_id(start)}, "
                f"end_id={describe_id(end)}; 허용 ID={ids[0]}~{ids[-1]} (정수, 양끝 포함). "
            )
            reason = None
            if type(start) is not int or type(end) is not int:
                reason = "ID는 문자열·소수·null이 아닌 JSON 정수여야 합니다."
            elif start not in known or end not in known:
                reason = "입력에 없는 ID입니다. 시간(초)이 아닌 입력 문장 ID를 사용하세요."
            elif start > end:
                reason = "start_id는 end_id 이하여야 합니다."
            if reason:
                raise LLMAnalysisError(f"{key} ID 범위가 올바르지 않습니다. {detail}{reason}")
            if start != expected:
                raise LLMAnalysisError(
                    f"{key} ID가 순서대로 전체 입력을 덮지 않습니다. "
                    f"{detail}이 항목의 start_id는 {expected}여야 합니다."
                )
            if require_chapter_fields and (
                not isinstance(item.get("summary"), str)
                or type(item.get("score")) is not int
                or not 0 <= item["score"] <= 1000
            ):
                raise LLMAnalysisError(
                    f"{key}의 summary 또는 score 형식이 올바르지 않습니다."
                )
            expected = end + 1
            result.append(item)
        if expected != ids[-1] + 1:
            raise LLMAnalysisError(
                f"{key}가 입력 마지막 ID까지 덮지 않습니다. "
                f"마지막 end_id={expected - 1}, 필요한 end_id={ids[-1]}."
            )
        return result

    @staticmethod
    def _jsonl(rows: list[dict[str, Any]]) -> str:
        return "\n".join(
            json.dumps(row, ensure_ascii=False, separators=(",", ":")) for row in rows
        )

    def split_unpunctuated_sentences(
        self, text: str, *, cancel_callback: Callable[[], None] | None = None
    ) -> list[int]:
        """원문 단어 ID로 문장 끝만 판별하고 문자 위치로 돌려준다."""
        words = list(re.finditer(r"\S+", text))
        if not words:
            return []
        if len(words) == 1:
            return [words[0].end()]
        cursor, window_size = 0, 240
        result = []
        while cursor < len(words):
            if cancel_callback:
                cancel_callback()
            stop = min(cursor + window_size, len(words))
            final = stop == len(words)
            # 비최종 창의 마지막 단어는 경계로 확정하지 않는다.
            maximum = stop - 1 if final else stop - 2
            schema = response_schema("sentence_boundary")
            array = schema["properties"]["end_ids"]
            array.update(minItems=1 if final else 0, maxItems=stop - cursor)
            array["items"].update(minimum=cursor, maximum=maximum)

            def validate(raw):
                if not isinstance(raw, dict) or set(raw) != {"end_ids"}:
                    raise LLMAnalysisError("문장 경계 응답은 end_ids 배열만 포함해야 합니다.")
                ends = raw["end_ids"]
                if (not isinstance(ends, list)
                        or any(type(i) is not int or not cursor <= i <= maximum for i in ends)
                        or ends != sorted(set(ends))):
                    raise LLMAnalysisError(
                        f"문장 end_ids는 {cursor}~{maximum} 범위의 중복 없는 오름차순 정수여야 합니다."
                    )
                if final and (not ends or ends[-1] != stop - 1):
                    raise LLMAnalysisError(f"마지막 문장 end_ids에 최종 단어 ID {stop - 1}을 포함하세요.")
                return ends

            ends = self._request_json(
                system_prompt("sentence_boundary"),
                json.dumps({
                    "is_final": final,
                    "words": [{"id": i, "text": words[i].group()} for i in range(cursor, stop)],
                }, ensure_ascii=False, separators=(",", ":")),
                response_schema=schema, validator=validate, cancel_callback=cancel_callback,
            )
            if not ends:
                if window_size >= 960:
                    raise LLMAnalysisError(
                        "구두점 없는 구간에서 문장 경계를 찾지 못했습니다. "
                        "원본 자막을 확인하세요. 임의 길이로 자르지는 않았습니다."
                    )
                window_size *= 2
                continue
            result.extend(words[i].end() for i in ends)
            cursor = ends[-1] + 1
            window_size = 240
        return result

    def structure_transcript(
        self,
        segments: list[dict[str, Any]],
        *,
        chapters: list[dict[str, Any]] | None = None,
        progress_callback: Callable[[int, int, str], None] | None = None,
        cancel_callback: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        rows = [{"id": int(item["id"]), "text": str(item["text"])} for item in segments]
        if not rows:
            raise LLMAnalysisError("구조화할 스크립트가 없습니다.")
        ids = [row["id"] for row in rows]
        if ids != list(range(ids[0], ids[-1] + 1)):
            raise LLMAnalysisError("입력 스크립트 ID가 연속적이지 않습니다.")
        if chapters is None:
            # 파일에서 매번 새 객체를 읽어 요청 사이에 ID 제한이 공유되지 않는다.
            chapter_schema = response_schema("chapter")
            chapter_array = chapter_schema["properties"]["chapters"]
            # Gemini는 큰 maxItems를 거부할 수 있다. 범위와 전체 문장 포함 여부는 로컬에서 검증한다.
            chapter_array["minItems"] = 1
            for field in ("start_id", "end_id"):
                chapter_array["items"]["properties"][field].update(
                    minimum=ids[0], maximum=ids[-1]
                )
            chapter_system = system_prompt("chapter") + (
                "\n입력 문장은 구두점 기준 분할과 구두점이 부족한 구간의 의미 기반 분할을 "
                "이미 마친 고정 단위다. 마침표가 없어도 완결된 문장일 수 있으며 다시 합치거나 쪼개지 않는다."
                f"\n\n[이번 요청의 ID 계약]\n입력 문장 수={len(ids)}, "
                f"허용 ID={ids[0]}~{ids[-1]} (양끝 포함). "
                "ID는 시간(초)이 아니라 입력 JSONL의 정수 id다. "
                f"첫 start_id={ids[0]}, 마지막 end_id={ids[-1]}. "
                "각 start_id <= end_id이며 다음 start_id는 앞 end_id+1이다. "
                "문자열·소수·null 또는 입력에 없는 ID를 반환하지 않는다."
            )
            if progress_callback:
                progress_callback(0, 1, "챕터 분할 요청")
            chapters = self._request_json(
                chapter_system,
                self._jsonl(rows),
                response_schema=chapter_schema,
                validator=lambda raw: self._validated_ranges(
                    raw, ids, key="chapters", require_chapter_fields=True
                ),
                cancel_callback=cancel_callback,
            )
            if progress_callback:
                progress_callback(1, 1, "챕터 분할 완료")

        # 입력 ID는 파이프라인에서 구두점·의미 기준으로 분리한 한 문장이다.
        # LLM은 상위 챕터만 결정하며 섹션 경계를 재분할하거나 병합하지 않는다.
        chapters = self._validated_ranges(
            {"chapters": chapters}, ids, key="chapters", require_chapter_fields=True
        )
        sections = []
        for index, chapter in enumerate(chapters):
            if cancel_callback:
                cancel_callback()
            sections.extend({"chapter_index": index, "start_id": row_id, "end_id": row_id}
                            for row_id in range(chapter["start_id"], chapter["end_id"] + 1))
            if progress_callback:
                progress_callback(index + 1, len(chapters), "문장 기준 섹션 구성")
        return {"chapters": chapters, "sections": sections}

    def detect_fillers(self, candidates, *, cancel_callback=None, progress_callback=None):
        """시간값을 LLM에 맡기지 않고 검증된 짧은 후보 ID만 판별한다."""
        removed = []
        for offset in range(0, len(candidates), 48):
            batch = candidates[offset:offset + 48]
            allowed = {item["id"] for item in batch}

            def validate(raw):
                if not isinstance(raw, dict) or set(raw) != {"remove_ids"}:
                    raise LLMAnalysisError("추임새 응답은 remove_ids 배열이어야 합니다.")
                ids = raw["remove_ids"]
                if (not isinstance(ids, list) or any(type(i) is not int or i not in allowed for i in ids)
                        or len(set(ids)) != len(ids)):
                    raise LLMAnalysisError("추임새 응답에 잘못되거나 중복된 후보 ID가 있습니다.")
                return ids

            ids = self._request_json(
                system_prompt("filler_detection"),
                json.dumps({"candidates": [
                    {key: item[key] for key in ("id", "text", "before", "after")} for item in batch
                ]}, ensure_ascii=False),
                response_schema=response_schema("filler_detection"),
                validator=validate, cancel_callback=cancel_callback,
            )
            removed.extend(item for item in batch if item["id"] in ids)
            if progress_callback:
                progress_callback(min(offset + 48, len(candidates)), len(candidates))
        return removed

    def score_sections(
        self,
        sections: list[dict[str, Any]],
        *,
        genre: str = "ai_news",
        criteria_prompt: str | None = None,
        progress_callback: Callable[[int, int, str], None] | None = None,
        cancel_callback: Callable[[], None] | None = None,
    ) -> list[dict[str, Any]]:
        if not sections:
            return []
        groups: dict[str, list[dict[str, Any]]] = {}
        for section in sections:
            groups.setdefault(str(section.get("chapter_id", "")), []).append(section)
        try:
            profile = user_prompt(criteria_prompt or genre)
        except PromptStoreError as exc:
            raise LLMAnalysisError(str(exc)) from exc
        system = system_prompt("section_score")
        limit = getattr(getattr(self, "gateway", None), "max_input_chars", 12000)
        batches = []
        for group in groups.values():
            # 챕터 요약과 동일 기준은 유지하고 응답 항목 수와 요청 길이를 제한한다.
            overhead = len(system) + len(json.dumps(profile, ensure_ascii=False)) + len(str(group[0].get("chapter_summary", ""))) + 1500
            budget = max(2000, limit - overhead)
            batch, size = [], 0
            for section in group:
                item_size = len(json.dumps(str(section.get("text", "")), ensure_ascii=False)) + 40
                if batch and (len(batch) >= 32 or size + item_size > budget):
                    batches.append(batch)
                    batch, size = [], 0
                # 문장이 긴 단일 섹션은 자르거나 누락하지 않고 단독 요청한다.
                batch.append(section)
                size += item_size
            if batch:
                batches.append(batch)

        def score(chapter_sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
            if cancel_callback:
                cancel_callback()
            payload = [{"id": index, "text": str(section.get("text", ""))}
                       for index, section in enumerate(chapter_sections)]
            expected = list(range(len(payload)))

            def validate(raw):
                if not isinstance(raw, dict) or set(raw) != {"items"}:
                    raise LLMAnalysisError("섹션 중요도 응답 객체 형식이 올바르지 않습니다.")
                items = raw["items"]
                if not isinstance(items, list) or len(items) != len(payload):
                    raise LLMAnalysisError(f"섹션 중요도 응답 개수가 입력 {len(payload)}개와 일치하지 않습니다.")
                if any(not isinstance(item, dict) or set(item) != {"id", "score"} for item in items):
                    raise LLMAnalysisError("섹션 중요도 항목 필드가 응답 계약과 다릅니다.")
                if any(type(item["id"]) is not int for item in items) or [item["id"] for item in items] != expected:
                    raise LLMAnalysisError("섹션 중요도 ID·순서가 입력과 일치하지 않습니다.")
                if any(type(item["score"]) is not int or not 0 <= item["score"] <= 1000 for item in items):
                    raise LLMAnalysisError("섹션 중요도 점수는 0~1000 정수여야 합니다.")
                return raw

            schema = response_schema("score")
            schema["properties"]["items"].update(minItems=len(payload), maxItems=len(payload))
            schema["properties"]["items"]["items"]["properties"]["id"]["enum"] = expected
            raw = self._request_json(
                system,
                json.dumps({
                    "criteria_profile": profile,
                    "chapter_summary": str(chapter_sections[0].get("chapter_summary", "")),
                    "sections": payload,
                }, ensure_ascii=False, separators=(",", ":")),
                response_schema=schema,
                validator=validate,
                cancel_callback=cancel_callback,
            )
            validate(raw)
            return [{**section, "llm_score": round(item["score"] / 1000, 3)}
                    for section, item in zip(chapter_sections, raw["items"])]

        result = []
        with ThreadPoolExecutor(
            max_workers=min(getattr(self, "_max_parallel_requests", DEFAULT_PARALLEL_REQUESTS), len(batches))
        ) as executor:
            futures = [executor.submit(score, batch) for batch in batches]
            for completed, future in enumerate(as_completed(futures), 1):
                if cancel_callback:
                    cancel_callback()
                result.extend(future.result())
                if progress_callback:
                    progress_callback(completed, len(futures), "섹션 중요도 묶음 평가")
        return sorted(result, key=lambda item: (
            str(item.get("chapter_id", "")), float(item.get("start", 0))
        ))

    def score_timestamp_comments(
        self,
        comments: list[dict[str, Any]],
        *,
        cancel_callback: Callable[[], None] | None = None,
    ) -> list[dict[str, int | float]]:
        rows = [
            {"index": index, "text": str(comment.get("text") or "").strip()}
            for index, comment in enumerate(comments)
        ]
        if not rows:
            return []

        def validate(raw: Any) -> list[dict[str, int | float]]:
            if not isinstance(raw, dict) or set(raw) != {"items"}:
                raise LLMAnalysisError(
                    "타임스탬프 댓글 점수 응답 객체 형식이 올바르지 않습니다."
                )
            items = raw.get("items")
            if not isinstance(items, list) or len(items) != len(rows):
                raise LLMAnalysisError(
                    "타임스탬프 댓글 점수 응답이 입력과 일치하지 않습니다."
                )
            if any(
                not isinstance(item, dict) or set(item) != {"index", "score"}
                for item in items
            ):
                raise LLMAnalysisError(
                    "타임스탬프 댓글 점수 항목 필드가 응답 계약과 다릅니다."
                )
            scores = {item.get("index"): item.get("score") for item in items}
            expected = list(range(len(rows)))
            if len(scores) != len(expected) or set(scores) != set(expected):
                raise LLMAnalysisError(
                    "타임스탬프 댓글 인덱스가 입력과 일치하지 않습니다."
                )
            if any(
                type(scores[index]) is not int or not 0 <= scores[index] <= 1000
                for index in expected
            ):
                raise LLMAnalysisError("타임스탬프 댓글 점수 형식이 올바르지 않습니다.")
            return [
                {"index": index, "score": round(float(scores[index]) / 1000, 3)}
                for index in expected
            ]

        return self._request_json(
            system_prompt("timestamp_comment_score"),
            json.dumps({"comments": rows}, ensure_ascii=False, separators=(",", ":")),
            response_schema=response_schema("comment_score"),
            validator=validate,
            cancel_callback=cancel_callback,
        )

    def required_anchor_links(
        self,
        anchor_id: str,
        chapter_summary: str,
        sections: list[dict[str, Any]],
        *,
        criteria_prompt: str = "",
        cancel_callback: Callable[[], None] | None = None,
    ) -> list[str]:
        ids = [str(item["id"]) for item in sections]
        if anchor_id not in ids:
            raise LLMAnalysisError("관계 확장 앵커가 챕터에 없습니다.")
        anchor_index = ids.index(anchor_id)
        if len(ids) == 1:
            return []
        aliases = {value: index for index, value in enumerate(ids)}
        def validate(raw: Any) -> list[str]:
            if not isinstance(raw, dict) or set(raw) != {"indexes"}:
                raise LLMAnalysisError("필수 관계 응답 형식이 올바르지 않습니다.")
            values = raw.get("indexes")
            if (
                not isinstance(values, list)
                or any(type(value) is not int for value in values)
                or values != sorted(set(values))
                or any(value < 0 or value >= len(ids) for value in values)
            ):
                raise LLMAnalysisError(
                    "필수 관계 인덱스 배열이 입력 섹션과 일치하지 않습니다."
                )
            return [ids[index] for index in values]

        payload = {
            "chapter_summary": chapter_summary,
            "anchor_id": anchor_index,
            "sections": [
                {
                    "id": aliases[str(item["id"])],
                    "text": str(item.get("text") or ""),
                }
                for item in sections
            ],
        }
        link_system = system_prompt("anchor_link")
        if criteria_prompt in SHORT_FORM_PROFILES:
            link_system += "\n\n" + system_prompt("short_form_context")
            payload["criteria_profile"] = user_prompt(criteria_prompt)
            for row, original in zip(payload["sections"], sections):
                for key in ("start", "end"):
                    if key in original:
                        row[key] = original[key]
        return self._request_json(
            link_system,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            response_schema=response_schema("anchor_link"),
            validator=validate,
            cancel_callback=cancel_callback,
        )

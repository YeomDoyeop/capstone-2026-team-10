"""공급자와 무관한 JSON 기반 영상 구조화·하이라이트 분석 서비스."""

from __future__ import annotations
import json
import hashlib
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable
from app.services.llm_gateway import LLMGateway, LLMGatewayError
from app.services.prompt_store import (
    PromptStoreError,
    response_schema,
    system_prompt,
    user_prompt,
)


class LLMAnalysisError(RuntimeError):
    """LLM 응답이 분석 계약을 지키지 않았을 때 발생한다."""


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

    def _request_json(
        self,
        system: str,
        prompt: str,
        *,
        response_schema: dict[str, Any] | None = None,
        validator: Callable[[Any], Any] | None = None,
        cancel_callback: Callable[[], None] | None = None,
    ) -> Any:
        cache_key = hashlib.sha256(
            json.dumps(
                [system, prompt, response_schema],
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
            value = validator(cached) if validator else cached
            if cancel_callback:
                cancel_callback()
            return value
        last_error: Exception | None = None
        for attempt in range(100):
            if cancel_callback:
                cancel_callback()
            self._wait_for_request_slot(cancel_callback)
            rule = (
                ""
                if attempt == 0
                else (
                    "\n직전 응답 거부 사유: "
                    + str(last_error)
                    + " 설명하지 말고 이 사유를 고쳐 완결된 JSON 객체 하나만 반환하세요."
                )
            )
            try:
                raw = json.loads(
                    self.gateway.request_json(
                        system + rule, prompt, response_schema=response_schema
                    )
                )
                value = validator(raw) if validator else raw
                if checkpoint_lock is not None:
                    with checkpoint_lock:
                        responses[cache_key] = raw
                        callback = getattr(self, "_checkpoint_callback", None)
                        if callback:
                            callback()
                if cancel_callback:
                    cancel_callback()
                return value
            except LLMGatewayError as exc:
                raise LLMAnalysisError(
                    f"구조화 JSON 요청에 실패했습니다: {exc}"
                ) from exc
            except (json.JSONDecodeError, LLMAnalysisError) as exc:
                last_error = exc
        detail = f": {last_error}" if last_error is not None else ""
        raise LLMAnalysisError(
            f"LLM이 백 번 연속 JSON 문법 또는 응답 계약을 지키지 않았습니다{detail}"
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
        for item in values:
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
            if (
                type(start) is not int
                or type(end) is not int
                or start not in known
                or end not in known
                or start > end
            ):
                raise LLMAnalysisError(f"{key} ID 범위가 올바르지 않습니다.")
            if start != expected:
                raise LLMAnalysisError(
                    f"{key} ID가 순서대로 전체 입력을 덮지 않습니다."
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
            raise LLMAnalysisError(f"{key}가 입력 마지막 ID까지 덮지 않습니다.")
        return result

    @staticmethod
    def _jsonl(rows: list[dict[str, Any]]) -> str:
        return "\n".join(
            json.dumps(row, ensure_ascii=False, separators=(",", ":")) for row in rows
        )

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
            if progress_callback:
                progress_callback(0, 1, "챕터 분할 요청")
            chapters = self._request_json(
                system_prompt("chapter"),
                self._jsonl(rows),
                response_schema=response_schema("chapter"),
                validator=lambda raw: self._validated_ranges(
                    raw, ids, key="chapters", require_chapter_fields=True
                ),
                cancel_callback=cancel_callback,
            )
            if progress_callback:
                progress_callback(1, 1, "챕터 분할 완료")

        def split(
            index: int, chapter: dict[str, Any]
        ) -> tuple[int, list[dict[str, Any]]]:
            if cancel_callback:
                cancel_callback()
            chapter_rows = [
                row
                for row in rows
                if chapter["start_id"] <= row["id"] <= chapter["end_id"]
            ]
            values = self._request_json(
                system_prompt("section"),
                self._jsonl(chapter_rows),
                response_schema=response_schema("section"),
                validator=lambda raw: self._validated_ranges(
                    raw,
                    [row["id"] for row in chapter_rows],
                    key="sections",
                    require_chapter_fields=False,
                ),
                cancel_callback=cancel_callback,
            )
            return index, values

        indexed = []
        with ThreadPoolExecutor(
            max_workers=min(getattr(self, "_max_parallel_requests", 100), len(chapters))
        ) as executor:
            futures = [
                executor.submit(split, index, chapter)
                for index, chapter in enumerate(chapters)
            ]
            for completed, future in enumerate(as_completed(futures), 1):
                if cancel_callback:
                    cancel_callback()
                indexed.append(future.result())
                if progress_callback:
                    progress_callback(completed, len(futures), "챕터별 섹션 분할")
        sections = []
        for index, values in sorted(indexed):
            sections.extend({"chapter_index": index, **section} for section in values)
        return {"chapters": chapters, "sections": sections}

    def score_sections(
        self,
        sections: list[dict[str, Any]],
        *,
        genre: str = "ai_news",
        criteria_prompt: str | None = None,
        progress_callback: Callable[[int, int, str], None] | None = None,
        cancel_callback: Callable[[], None] | None = None,
    ) -> list[dict[str, Any]]:
        groups: dict[str, list[dict[str, Any]]] = {}
        for section in sections:
            groups.setdefault(str(section.get("chapter_id", "")), []).append(section)
        try:
            profile = user_prompt(criteria_prompt or genre)
        except PromptStoreError as exc:
            raise LLMAnalysisError(str(exc)) from exc
        system = system_prompt("section_score")

        def score(chapter_sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
            if cancel_callback:
                cancel_callback()
            payload = [
                {
                    "id": index,
                    "text": str(section.get("text", "")),
                }
                for index, section in enumerate(chapter_sections)
            ]
            raw = self._request_json(
                system,
                json.dumps(
                    {
                        "criteria_profile": profile,
                        "chapter_summary": str(
                            chapter_sections[0].get("chapter_summary", "")
                        ),
                        "sections": payload,
                    },
                    ensure_ascii=False,
                ),
                response_schema=response_schema("score"),
                cancel_callback=cancel_callback,
            )
            if not isinstance(raw, dict) or set(raw) != {"items"}:
                raise LLMAnalysisError(
                    "섹션 중요도 응답 객체 형식이 올바르지 않습니다."
                )
            items = raw.get("items")
            if not isinstance(items, list) or len(items) != len(chapter_sections):
                raise LLMAnalysisError("섹션 중요도 응답이 입력과 일치하지 않습니다.")
            if any(
                not isinstance(item, dict) or set(item) != {"id", "score"}
                for item in items
            ):
                raise LLMAnalysisError("섹션 중요도 항목 필드가 응답 계약과 다릅니다.")
            scores = {
                item.get("id"): item.get("score")
                for item in items
                if isinstance(item, dict)
            }
            expected = [item["id"] for item in payload]
            if len(scores) != len(expected) or set(scores) != set(expected):
                raise LLMAnalysisError("섹션 중요도 ID가 입력과 일치하지 않습니다.")
            if any(
                type(scores[item_id]) is not int or not 0 <= scores[item_id] <= 1000
                for item_id in expected
            ):
                raise LLMAnalysisError("섹션 중요도 점수 형식이 올바르지 않습니다.")
            return [
                {**section, "llm_score": round(float(scores[item["id"]]) / 1000, 3)}
                for section, item in zip(chapter_sections, payload)
            ]

        result = []
        with ThreadPoolExecutor(
            max_workers=min(getattr(self, "_max_parallel_requests", 100), len(groups))
        ) as executor:
            futures = [executor.submit(score, group) for group in groups.values()]
            for completed, future in enumerate(as_completed(futures), 1):
                if cancel_callback:
                    cancel_callback()
                result.extend(future.result())
                if progress_callback:
                    progress_callback(
                        completed, len(futures), "챕터별 섹션 중요도 평가"
                    )
        return sorted(
            result,
            key=lambda item: (
                str(item.get("chapter_id", "")),
                float(item.get("start", 0)),
            ),
        )

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
        return self._request_json(
            system_prompt("anchor_link"),
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            response_schema=response_schema("anchor_link"),
            validator=validate,
            cancel_callback=cancel_callback,
        )

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

class LLMAnalysisError(RuntimeError):
    """LLM 응답이 분석 계약을 지키지 않았을 때 발생한다."""

GENRE_GUIDES = {
    "ai_news": "AI 뉴스: 높은 점수는 실제 새 발표·정책·제품·연구·사건, 모델명·기관·수치, 원인→영향→결론 연결이다. 낮은 점수는 광고·반복·침묵·근거 없는 전망이다. 사실과 진행자 의견·추측을 구분한다.",
    "stock": "주식·증시: 높은 점수는 기업·산업·거시 사건, 실적·수치, 촉매·위험, 근거→결론 연결이다. 낮은 점수는 광고·종목 나열·반복·침묵·무근거 확신이다. 사실과 진행자 의견·투자 추측을 구분한다.",
    "game": "게임: 높은 점수는 전략의 전제→실행→결과, 패치·시스템 변화, 전문 용어, 중요한 판단·전환점·반전이다. 낮은 점수는 광고·반복·침묵·근거 없는 과장이다. 사실과 플레이 감상·추측을 구분한다.",
}
CHAPTER_SYSTEM = '''역할: 낮은 오류 허용도의 영상 편집용 스크립트 구조화기.
목표: 입력 JSONL 전체를 시간순으로 완전 분할한 챕터 JSON만 반환한다.
입력 형식: 각 줄은 {"id": 정수, "text": 문자열}이며 id는 시간순이다.
판단 기준: 실제 주제 전환, 논점 변화, 사건 흐름, 결론을 기준으로만 나눈다. 균등 분할, 단순 시간 기준 분할, 임의 챕터 수는 금지한다.
사실성 금지: 입력 text 밖의 사실·시간·원인·결론·id를 만들지 않는다.
범위·검증 규칙: 제공된 JSONL id만 사용한다. 첫 항목 start_id는 첫 입력 id, 다음 항목 start_id는 바로 앞 end_id+1, 마지막 end_id는 마지막 입력 id다. 빈틈·겹침·중복·누락·역순은 금지한다.
summary: 이 챕터에서 실제로 말한 내용을 160자 이내로 요약한다. 판단할 수 없으면 빈 문자열을 쓴다.
score: 이 챕터가 최종 편집에서 갖는 중요도를 0~1000 정수로 평가한다. 900~1000은 영상의 핵심 결론·사건·반전, 700~899는 핵심 맥락·근거·시작 또는 종료 인사, 400~699는 유용한 보조 내용, 0~399는 광고·반복·침묵·무근거 주장이다. 관습적인 앵커값에 고정하지 말고 챕터 간 상대적 차이를 일의 자리까지 세밀하게 반영한다.
출력 규칙: JSON 외 텍스트, Markdown, 코드펜스, 설명, 주석을 절대 쓰지 말고 아래 JSON 객체만 반환한다.
출력 형식: {"chapters":[{"start_id":number,"end_id":number,"summary":string,"score":number}]}'''
SECTION_SYSTEM = '''역할: 영상 편집용 챕터 내부 최소 의미 단위 분할기.
목표: 한 주장·근거·사건·설명이 끝나는 최소 연속 범위로 입력 전체를 분할한다.
입력 형식: 각 줄은 {"id": 정수, "text": 문자열} JSONL이다.
판단·금지: 문장마다 기계적으로 쪼개기, 서로 다른 논점을 한 범위로 과도하게 합치기, 입력 밖 사실·시간·id 추가를 금지한다.
범위·검증 규칙: 제공된 id만 사용한다. 첫 항목 start_id는 첫 입력 id, 다음 항목 start_id는 바로 앞 end_id+1, 마지막 end_id는 마지막 입력 id다. 빈틈·겹침·중복·누락·역순은 금지한다.
출력 규칙: JSON 외 텍스트, Markdown, 코드펜스, 설명, 주석을 절대 쓰지 말고 아래 JSON 객체만 반환한다.
출력 형식: {"sections":[{"start_id":number,"end_id":number}]}'''
CHAPTER_RESPONSE_SCHEMA={"type":"object","additionalProperties":False,"required":["chapters"],"properties":{"chapters":{"type":"array","items":{"type":"object","additionalProperties":False,"required":["start_id","end_id","summary","score"],"properties":{"start_id":{"type":"integer"},"end_id":{"type":"integer"},"summary":{"type":"string"},"score":{"type":"integer","minimum":0,"maximum":1000}}}}}}
SECTION_RESPONSE_SCHEMA={"type":"object","additionalProperties":False,"required":["sections"],"properties":{"sections":{"type":"array","items":{"type":"object","additionalProperties":False,"required":["start_id","end_id"],"properties":{"start_id":{"type":"integer"},"end_id":{"type":"integer"}}}}}}
SCORE_RESPONSE_SCHEMA={"type":"object","additionalProperties":False,"required":["items"],"properties":{"items":{"type":"array","items":{"type":"object","additionalProperties":False,"required":["id","score"],"properties":{"id":{"type":"string"},"score":{"type":"integer","minimum":0,"maximum":1000}}}}}}
WHISPER_SETTINGS_SYSTEM = '''역할: 영상 메타데이터에서 Whisper STT용 고유명사 핫워드만 보수적으로 추출하는 개체명 판별기.

고유명사 판정:
1. 항목은 현실 또는 작품 안의 단 하나의 특정 대상을 식별하는 이름이어야 한다.
2. 허용 유형은 person, organization, channel, brand, product, model, service, work, game, place, event 중 하나다.
3. 입력 메타데이터에 동일한 문자열이 원문 그대로 존재해야 한다. 번역·교정·확장·축약·추측으로 표기를 만들지 않는다.
4. 일반 명사, 보편적 전문 용어, 직업명, 직함만 있는 표현, 학문·산업 분야, 주제, 장르, 범주, 성질, 상태, 행동, 설명구는 제외한다.
5. 문맥에 따라 고유명사일 수도 있는 애매한 말은 제외한다. 포함 근거가 명백한 항목만 선택한다.
6. 동일 대상을 가리키는 전체명·약칭·부분명·표기 변형이 겹치면 가장 명확한 원문 표기 하나만 남긴다.
7. 항목 수를 채우지 않는다. 확실한 고유명사가 없으면 빈 배열을 반환한다.

작업 순서:
1. 입력에서 후보를 찾는다.
2. 각 후보가 특정 대상의 이름인지 판정한다.
3. 허용 유형 하나를 지정한다.
4. 원문 일치와 중복 여부를 다시 확인한다.
5. 확실한 항목만 중요도순으로 최대 10개 반환한다. 각 text는 40자 이내다.

출력 전 검사: 각 항목에 대해 “이 표현은 종류나 개념이 아니라 특정 대상의 이름인가?”에 확실히 예라고 답할 수 없으면 삭제한다.
출력 규칙: JSON 외 텍스트, Markdown, 코드펜스, 설명, 주석을 절대 쓰지 않는다.
출력 형식: {"hotwords":[{"text":string,"entity_type":string}]}'''
WHISPER_ENTITY_TYPES = frozenset({"person", "organization", "channel", "brand", "product", "model", "service", "work", "game", "place", "event"})
WHISPER_SETTINGS_RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["hotwords"],
    "properties": {
        "hotwords": {"type": "array", "maxItems": 10, "items": {
            "type": "object", "additionalProperties": False, "required": ["text", "entity_type"],
            "properties": {"text": {"type": "string", "minLength": 1, "maxLength": 40}, "entity_type": {"type": "string", "enum": sorted(WHISPER_ENTITY_TYPES)}},
        }},
    },
}
SUBTITLE_SPLIT_SYSTEM = '''역할: 영상 자막용 문장 경계 결정기.
목표: 하나의 긴 Whisper 문장을 제공된 단어 경계에서만 의미가 자연스럽고 길이가 가능한 한 균등한 연속 자막으로 분할한다.
입력 형식: {"target_count":정수,"words":[{"id":0부터 시작하는 연속 정수,"text":원문 단어}]}이다.
분할 기준: target_count개를 정확히 만들되 각 부분의 공백 제외 글자 수가 20자에 가깝고 서로 가능한 한 균등해야 한다. 구·절·문장부호와 문법적 의미 단위를 우선하며, 지나치게 짧은 조각을 만들지 않는다.
경계 규칙: indexes에는 각 자막 조각의 마지막 단어 id를 마지막 조각을 제외하고 순서대로 넣는다. 예를 들어 indexes가 [0,2]이면 단어 범위는 0 / 1~2 / 3~마지막이다. 값은 0 이상 마지막 단어 id 미만의 서로 다른 오름차순 정수이며, 개수는 target_count-1이어야 한다.
보존 규칙: 단어를 수정·추가·삭제·재배열하지 않는다.
출력 규칙: JSON 외 텍스트, Markdown, 코드펜스, 설명, 주석을 절대 쓰지 않는다.
출력 형식: {"indexes":[number]}'''
SUBTITLE_SPLIT_RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["indexes"],
    "properties": {
        "indexes": {"type": "array", "items": {"type": "integer"}},
    },
}
class LLMAnalysisService:
    def __init__(self, *, provider: str="deepseek", server_access_token: str|None=None, checkpoint: dict[str, Any] | None = None, checkpoint_callback: Callable[[], None] | None = None, **_: Any):
        try: self.gateway=LLMGateway(provider, server_access_token=server_access_token)
        except LLMGatewayError as exc: raise LLMAnalysisError(str(exc)) from exc
        self._max_parallel_requests = self.gateway.max_parallel_requests
        self._minimum_request_interval_seconds = self.gateway.minimum_request_interval_seconds
        self._request_limit_lock = threading.Lock()
        self._last_request_started_at = 0.0
        self._checkpoint = checkpoint if checkpoint is not None else {}
        self._checkpoint_responses = self._checkpoint.setdefault("llm_responses", {})
        self._checkpoint_callback = checkpoint_callback
        self._checkpoint_lock = threading.Lock()
    def _wait_for_request_slot(self, cancel_callback: Callable[[],None]|None) -> None:
        interval = getattr(self, "_minimum_request_interval_seconds", 0.0)
        if interval <= 0:
            return
        with self._request_limit_lock:
            remaining = interval - (time.monotonic() - self._last_request_started_at)
            while remaining > 0:
                if cancel_callback: cancel_callback()
                time.sleep(min(0.25, remaining))
                remaining = interval - (time.monotonic() - self._last_request_started_at)
            if cancel_callback: cancel_callback()
            self._last_request_started_at = time.monotonic()
    def _request_json(self, system: str, prompt: str, *, response_schema: dict[str,Any]|None=None, validator: Callable[[Any],Any]|None=None, cancel_callback: Callable[[],None]|None=None) -> Any:
        cache_key = hashlib.sha256(json.dumps([system, prompt, response_schema], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        checkpoint_lock = getattr(self, "_checkpoint_lock", None)
        responses = getattr(self, "_checkpoint_responses", {})
        if checkpoint_lock is None:
            cached = None
        else:
            with checkpoint_lock:
                cached = responses.get(cache_key)
        if cached is not None:
            value = validator(cached) if validator else cached
            if cancel_callback: cancel_callback()
            return value
        last_error: Exception|None=None
        for attempt in range(20):
            if cancel_callback: cancel_callback()
            self._wait_for_request_slot(cancel_callback)
            rule="" if attempt==0 else "\n직전 응답은 JSON 문법 또는 배열 길이·ID 범위 계약을 지키지 못했습니다. 설명하지 말고 완결된 JSON 객체 하나만 반환하세요. 입력 순서와 길이, 문자열·쉼표·대괄호·중괄호를 확인하세요."
            try:
                raw=json.loads(self.gateway.request_json(system+rule,prompt,response_schema=response_schema))
                value = validator(raw) if validator else raw
                if checkpoint_lock is not None:
                    with checkpoint_lock:
                        responses[cache_key] = raw
                        callback = getattr(self, "_checkpoint_callback", None)
                        if callback:
                            callback()
                if cancel_callback: cancel_callback()
                return value
            except LLMGatewayError as exc: raise LLMAnalysisError(f"구조화 JSON 요청에 실패했습니다: {exc}") from exc
            except (json.JSONDecodeError,LLMAnalysisError) as exc: last_error=exc
        raise LLMAnalysisError("LLM이 스무 번 연속 JSON 문법 또는 응답 계약을 지키지 않았습니다.") from last_error
    @staticmethod
    def _validated_whisper_settings(raw: Any, *, metadata_text: str = "") -> dict[str, Any]:
        if not isinstance(raw, dict) or set(raw) != {"hotwords"}:
            raise LLMAnalysisError("Whisper 설정 응답 객체 형식이 올바르지 않습니다.")
        hotwords = raw.get("hotwords")
        if not isinstance(hotwords, list) or len(hotwords) > 10:
            raise LLMAnalysisError("Whisper 설정 응답 필드 형식이 올바르지 않습니다.")
        cleaned: list[str] = []
        seen: set[str] = set()
        for item in hotwords:
            if not isinstance(item, dict) or set(item) != {"text", "entity_type"}:
                raise LLMAnalysisError("Whisper 핫워드는 고유명사와 개체 유형을 포함해야 합니다.")
            value, entity_type = item.get("text"), item.get("entity_type")
            if not isinstance(value, str) or entity_type not in WHISPER_ENTITY_TYPES:
                raise LLMAnalysisError("Whisper 핫워드의 고유명사 유형이 올바르지 않습니다.")
            word = value.strip()
            if not word or len(word) > 40:
                raise LLMAnalysisError("Whisper 핫워드 길이가 올바르지 않습니다.")
            if metadata_text and word.casefold() not in metadata_text.casefold():
                raise LLMAnalysisError("Whisper 핫워드는 입력 메타데이터의 원문 표기여야 합니다.")
            key = word.casefold()
            if key not in seen:
                seen.add(key)
                cleaned.append(word)
        return {"hotwords": ", ".join(cleaned)}
    def recommend_whisper_settings(self, metadata: dict[str, Any]) -> dict[str, Any]:
        prompt = json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
        return self._request_json(
            WHISPER_SETTINGS_SYSTEM,
            prompt,
            response_schema=WHISPER_SETTINGS_RESPONSE_SCHEMA,
            validator=lambda raw: self._validated_whisper_settings(raw, metadata_text=prompt),
        )
    def split_subtitle_words(
        self,
        words: list[dict[str, Any]],
        target_count: int,
        *,
        cancel_callback: Callable[[], None] | None = None,
    ) -> list[dict[str, int]]:
        rows = [{"id": index, "text": str(word["word"])} for index, word in enumerate(words)]
        if target_count < 2 or target_count > len(rows):
            raise LLMAnalysisError("자막 목표 분할 수가 단어 수와 맞지 않습니다.")

        def validate(raw: Any) -> list[dict[str, int]]:
            if not isinstance(raw, dict) or set(raw) != {"indexes"}:
                raise LLMAnalysisError("자막 분할 응답 객체 형식이 올바르지 않습니다.")
            indexes = raw.get("indexes")
            if (
                not isinstance(indexes, list)
                or len(indexes) != target_count - 1
                or any(type(index) is not int for index in indexes)
                or indexes != sorted(set(indexes))
                or any(index < 0 or index >= len(rows) - 1 for index in indexes)
            ):
                raise LLMAnalysisError("자막 분할 경계 인덱스가 올바르지 않습니다.")
            starts = [0, *(index + 1 for index in indexes)]
            ends = [*indexes, len(rows) - 1]
            return [
                {"start_word": start, "end_word": end}
                for start, end in zip(starts, ends)
            ]

        prompt = json.dumps({"target_count": target_count, "words": rows}, ensure_ascii=False, separators=(",", ":"))
        return self._request_json(
            SUBTITLE_SPLIT_SYSTEM,
            prompt,
            response_schema=SUBTITLE_SPLIT_RESPONSE_SCHEMA,
            validator=validate,
            cancel_callback=cancel_callback,
        )
    @staticmethod
    def _validated_ranges(raw: Any, ids: list[int], *, key: str, require_chapter_fields: bool) -> list[dict[str,Any]]:
        if not isinstance(raw,dict) or set(raw) != {key}:
            raise LLMAnalysisError(f"{key} 응답 객체 형식이 올바르지 않습니다.")
        values=raw.get(key)
        if not isinstance(values,list) or not values: raise LLMAnalysisError(f"{key} 응답 형식이 올바르지 않습니다.")
        expected=ids[0]; known=set(ids); result=[]
        for item in values:
            if not isinstance(item,dict): raise LLMAnalysisError(f"{key} 항목 형식이 올바르지 않습니다.")
            expected_fields={"start_id","end_id","summary","score"} if require_chapter_fields else {"start_id","end_id"}
            if set(item) != expected_fields: raise LLMAnalysisError(f"{key} 항목 필드가 응답 계약과 다릅니다.")
            start,end=item.get("start_id"),item.get("end_id")
            if type(start) is not int or type(end) is not int or start not in known or end not in known or start>end: raise LLMAnalysisError(f"{key} ID 범위가 올바르지 않습니다.")
            if start!=expected: raise LLMAnalysisError(f"{key} ID가 순서대로 전체 입력을 덮지 않습니다.")
            if require_chapter_fields and (not isinstance(item.get("summary"),str) or type(item.get("score")) is not int or not 0 <= item["score"] <= 1000): raise LLMAnalysisError(f"{key}의 summary 또는 score 형식이 올바르지 않습니다.")
            expected=end+1; result.append(item)
        if expected!=ids[-1]+1: raise LLMAnalysisError(f"{key}가 입력 마지막 ID까지 덮지 않습니다.")
        return result
    @staticmethod
    def _jsonl(rows: list[dict[str,Any]]) -> str: return "\n".join(json.dumps(row,ensure_ascii=False,separators=(",",":")) for row in rows)
    def structure_transcript(self, segments: list[dict[str,Any]], *, progress_callback: Callable[[int,int,str],None]|None=None, cancel_callback: Callable[[],None]|None=None) -> dict[str,Any]:
        rows=[{"id":int(item["id"]),"text":str(item["text"])} for item in segments]
        if not rows: raise LLMAnalysisError("구조화할 스크립트가 없습니다.")
        ids=[row["id"] for row in rows]
        if ids!=list(range(ids[0],ids[-1]+1)): raise LLMAnalysisError("입력 스크립트 ID가 연속적이지 않습니다.")
        if progress_callback: progress_callback(0, 1, "챕터 분할 요청")
        chapters=self._request_json(CHAPTER_SYSTEM,self._jsonl(rows),response_schema=CHAPTER_RESPONSE_SCHEMA,validator=lambda raw:self._validated_ranges(raw,ids,key="chapters",require_chapter_fields=True),cancel_callback=cancel_callback)
        if progress_callback: progress_callback(1, 1, "챕터 분할 완료")
        def split(index: int, chapter: dict[str,Any]) -> tuple[int,list[dict[str,Any]]]:
            if cancel_callback: cancel_callback()
            chapter_rows=[row for row in rows if chapter["start_id"]<=row["id"]<=chapter["end_id"]]
            values=self._request_json(SECTION_SYSTEM,self._jsonl(chapter_rows),response_schema=SECTION_RESPONSE_SCHEMA,validator=lambda raw:self._validated_ranges(raw,[row["id"] for row in chapter_rows],key="sections",require_chapter_fields=False),cancel_callback=cancel_callback)
            return index, values
        indexed=[]
        with ThreadPoolExecutor(max_workers=min(getattr(self, "_max_parallel_requests", 100),len(chapters))) as executor:
            futures=[executor.submit(split,index,chapter) for index,chapter in enumerate(chapters)]
            for completed,future in enumerate(as_completed(futures),1):
                if cancel_callback: cancel_callback()
                indexed.append(future.result())
                if progress_callback: progress_callback(completed,len(futures),"챕터별 섹션 분할")
        sections=[]
        for index,values in sorted(indexed): sections.extend({"chapter_index":index,**section} for section in values)
        return {"chapters":chapters,"sections":sections}
    def score_sections(self, sections: list[dict[str,Any]], *, genre: str="ai_news", progress_callback: Callable[[int,int,str],None]|None=None, cancel_callback: Callable[[],None]|None=None) -> list[dict[str,Any]]:
        groups: dict[str,list[dict[str,Any]]]={}
        for section in sections: groups.setdefault(str(section.get("chapter_id","")),[]).append(section)
        system=f'''역할: {genre} 영상의 섹션 단위 편집 하이라이트 평가자.
목표: 한 챕터의 각 입력 섹션 중요도를 입력 순서대로 0~1000 정수로 평가한다.
{GENRE_GUIDES.get(genre,GENRE_GUIDES["ai_news"])}
점수 기준: 900~1000은 핵심 사실·결론·반전 또는 반드시 필요한 근거, 700~899는 맥락·원인·결과를 보존하는 중요 설명과 시작·종료 인사, 400~699는 유용하지만 생략 가능한 보조 내용, 0~399는 광고·반복·침묵·추측·무근거 주장이다. 관습적인 앵커 숫자에 고정하지 말고 섹션 간 상대적 중요도를 비교해 일의 자리까지 세밀한 정수를 사용한다.
시작 인사·영상 주제 소개·종료 인사는 높은 점수를 준다. 입력 밖 사실·시간·원인·결론을 만들지 않는다.
챕터 요약: 아래 입력에는 이 섹션들이 속한 챕터의 입력 기반 summary가 함께 제공된다. 이 요약을 기준으로 챕터 안에서 핵심 주장·근거·결론을 우선 판별하되, 요약이나 섹션 본문에 없는 사실은 만들지 않는다.
입력 형식: {{"chapter_summary":string,"sections":[{{"id":섹션 ID,"text":섹션 본문}}]}}이다. 제공된 id를 바꾸거나 새로 만들거나 누락하지 말고, 순서를 바꾸거나 섹션을 합치거나 나누지 않는다.
검증: items 배열은 입력과 길이가 같고 각 id를 중복·누락 없이 정확히 한 번 사용한다. score는 0~1000 정수다.
출력: JSON 외 텍스트, Markdown, 코드펜스, 설명을 금지한다. {{"items":[{{"id":string,"score":number}}]}}만 반환한다. 이유·다른 필드는 반환하지 않는다.'''
        def score(chapter_sections: list[dict[str,Any]]) -> list[dict[str,Any]]:
            if cancel_callback: cancel_callback()
            payload=[{"id":str(section.get("section_id") or section.get("segment_id") or ""),"text":str(section.get("text",""))} for section in chapter_sections]
            raw=self._request_json(system,json.dumps({"chapter_summary":str(chapter_sections[0].get("chapter_summary", "")),"sections":payload},ensure_ascii=False),response_schema=SCORE_RESPONSE_SCHEMA,cancel_callback=cancel_callback)
            if not isinstance(raw,dict) or set(raw) != {"items"}: raise LLMAnalysisError("섹션 중요도 응답 객체 형식이 올바르지 않습니다.")
            items=raw.get("items")
            if not isinstance(items,list) or len(items)!=len(chapter_sections): raise LLMAnalysisError("섹션 중요도 응답이 입력과 일치하지 않습니다.")
            if any(not isinstance(item,dict) or set(item)!={"id","score"} for item in items): raise LLMAnalysisError("섹션 중요도 항목 필드가 응답 계약과 다릅니다.")
            scores={item.get("id"):item.get("score") for item in items if isinstance(item,dict)}
            expected=[item["id"] for item in payload]
            if len(scores)!=len(expected) or set(scores)!=set(expected): raise LLMAnalysisError("섹션 중요도 ID가 입력과 일치하지 않습니다.")
            if any(type(scores[item_id]) is not int or not 0<=scores[item_id]<=1000 for item_id in expected): raise LLMAnalysisError("섹션 중요도 점수 형식이 올바르지 않습니다.")
            return [{**section,"llm_score":float(scores[item["id"]])} for section,item in zip(chapter_sections,payload)]
        result=[]
        with ThreadPoolExecutor(max_workers=min(getattr(self, "_max_parallel_requests", 100),len(groups))) as executor:
            futures=[executor.submit(score, group) for group in groups.values()]
            for completed,future in enumerate(as_completed(futures),1):
                if cancel_callback: cancel_callback()
                result.extend(future.result())
                if progress_callback: progress_callback(completed,len(futures),"챕터별 섹션 중요도 평가")
        return sorted(result,key=lambda item:(str(item.get("chapter_id","")),float(item.get("start",0))))

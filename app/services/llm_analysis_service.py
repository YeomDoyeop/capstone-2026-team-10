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
목표: 입력 JSONL 전체를 시간순으로 완전 분할하되, 챕터는 뒤의 세부 섹션보다 명확히 큰 상위 주제 단위로 만든다.
입력 형식: 각 줄은 {"id": 정수, "text": 문자열}이며 id는 시간순이다.
큰 단위 원칙: 하나의 중심 질문·사건·논점·목표를 다루는 동안에는 설명, 질문과 답변, 주장과 근거, 사례, 반론, 결과와 결론이 이어져도 같은 챕터로 유지한다. 같은 대상을 더 자세히 설명하거나 말투·화자·소주제가 바뀌는 것만으로 챕터를 나누지 않는다.
분할 허용: 이후 내용의 중심 질문·사건·논점·목표가 이전 내용과 분명히 달라져, 앞 챕터의 맥락 없이도 별개의 큰 주제로 설명될 때만 경계를 만든다.
분할 금지: 질문과 답변 사이, 주장과 필수 근거 사이, 원인과 직접 결과 사이, 도입과 그 설명 사이를 자르지 않는다. 균등 분할, 단순 시간·길이 기준, 문장 수 맞추기, 잦은 소제목 생성, 임의 챕터 수를 금지한다. 애매하면 나누지 않고 큰 챕터로 유지한다.
자기 점검: 인접한 두 챕터를 같은 한 문장 제목으로 자연스럽게 요약할 수 있다면 합친다. 한 챕터가 단일 답변·사례·짧은 설명에 불과하면 상위 주제가 같은 인접 챕터에 합친다.
사실성 금지: 입력 text 밖의 사실·시간·원인·결론·id를 만들지 않는다.
범위·검증 규칙: 제공된 JSONL id만 사용한다. 첫 항목 start_id는 첫 입력 id, 다음 항목 start_id는 바로 앞 end_id+1, 마지막 end_id는 마지막 입력 id다. 빈틈·겹침·중복·누락·역순은 금지한다.
summary: 이 챕터에서 실제로 말한 내용을 160자 이내로 요약한다. 판단할 수 없으면 빈 문자열을 쓴다.
score: 이 챕터가 최종 편집에서 갖는 중요도를 0~1000 정수로 평가한다. 900~1000은 영상의 핵심 결론·사건·반전, 700~899는 핵심 맥락·근거·시작 또는 종료 인사, 400~699는 유용한 보조 내용, 0~399는 광고·반복·침묵·무근거 주장이다. 관습적인 앵커값에 고정하지 말고 챕터 간 상대적 차이를 일의 자리까지 세밀하게 반영한다.
출력 규칙: JSON 외 텍스트, Markdown, 코드펜스, 설명, 주석을 절대 쓰지 말고 아래 JSON 객체만 반환한다.
출력 형식: {"chapters":[{"start_id":number,"end_id":number,"summary":string,"score":number}]}'''
SECTION_SYSTEM = '''역할: 영상 편집용 챕터 내부 최소 의미 단위 분할기.
목표: 챕터 안의 입력 전체를 각각 독립적으로 선택·제외할 수 있는 가능한 한 작은 의미 단위로 완전 분할한다.
입력 형식: 각 줄은 {"id": 정수, "text": 문자열} JSONL이다.
작은 단위 원칙: 질문, 직접 답변, 주장, 개별 근거, 사례, 반론, 원인, 결과, 결론처럼 편집 판단이 달라질 수 있는 내용은 각각 별도 섹션으로 분리한다. 하나의 기능만 수행하는 최소 연속 범위를 선호한다.
경계 판단: 다음 입력부터 담화 기능이나 독립적으로 평가할 의미가 바뀌면 경계를 만든다. 한 섹션 안의 일부를 제거해도 나머지 의미가 성립한다면 제거 가능한 부분을 별도 섹션으로 나눈다.
과분할 금지: 문장이 문법적으로 끝나지 않았거나 대명사·접속어·수식어만 남는 경계, 하나의 짧은 주장이나 답변을 문장마다 기계적으로 자르는 경계는 만들지 않는다. 최소 단위는 단독으로 읽었을 때 역할을 판별할 수 있어야 한다.
과소분할 금지: 질문과 답변, 주장과 근거, 원인과 결과, 서로 다른 사례를 하나의 큰 섹션으로 합치지 않는다. 여러 기능이 들어 있으면 각 기능의 끝에서 나눈다.
자기 점검: 각 섹션에 질문·답변·주장·근거·사례·원인·결과·결론 중 주된 역할 하나를 붙일 수 있는지 확인한다. 두 역할 이상이면 가능한 의미 경계에서 더 나눈다.
입력 밖 사실·시간·id 추가를 금지한다.
범위·검증 규칙: 제공된 id만 사용한다. 첫 항목 start_id는 첫 입력 id, 다음 항목 start_id는 바로 앞 end_id+1, 마지막 end_id는 마지막 입력 id다. 빈틈·겹침·중복·누락·역순은 금지한다.
출력 규칙: JSON 외 텍스트, Markdown, 코드펜스, 설명, 주석을 절대 쓰지 말고 아래 JSON 객체만 반환한다.
출력 형식: {"sections":[{"start_id":number,"end_id":number}]}'''
CHAPTER_RESPONSE_SCHEMA={"type":"object","additionalProperties":False,"required":["chapters"],"properties":{"chapters":{"type":"array","items":{"type":"object","additionalProperties":False,"required":["start_id","end_id","summary","score"],"properties":{"start_id":{"type":"integer"},"end_id":{"type":"integer"},"summary":{"type":"string"},"score":{"type":"integer","minimum":0,"maximum":1000}}}}}}
SECTION_RESPONSE_SCHEMA={"type":"object","additionalProperties":False,"required":["sections"],"properties":{"sections":{"type":"array","items":{"type":"object","additionalProperties":False,"required":["start_id","end_id"],"properties":{"start_id":{"type":"integer"},"end_id":{"type":"integer"}}}}}}
SCORE_RESPONSE_SCHEMA={"type":"object","additionalProperties":False,"required":["items"],"properties":{"items":{"type":"array","items":{"type":"object","additionalProperties":False,"required":["id","score"],"properties":{"id":{"type":"string"},"score":{"type":"integer","minimum":0,"maximum":1000}}}}}}
COMMENT_SCORE_RESPONSE_SCHEMA={"type":"object","additionalProperties":False,"required":["items"],"properties":{"items":{"type":"array","items":{"type":"object","additionalProperties":False,"required":["index","score"],"properties":{"index":{"type":"integer","minimum":0},"score":{"type":"integer","minimum":0,"maximum":1000}}}}}}
ANCHOR_LINK_RESPONSE_SCHEMA={"type":"object","additionalProperties":False,"required":["before_ids","after_ids"],"properties":{"before_ids":{"type":"array","maxItems":2,"items":{"type":"string"}},"after_ids":{"type":"array","maxItems":2,"items":{"type":"string"}}}}
ANCHOR_LINK_SYSTEM = '''역할: 영상 요약 편집의 최소 필수 관계 판정기.
최우선 목표: anchor는 이미 반드시 포함되는 핵심 하이라이트다. anchor를 대체하거나 제외할 대상을 고르지 말고, 시청자가 앞뒤 원본을 보지 않아도 anchor의 맥락과 결말을 자연스럽게 이해할 수 있는 최소 편집 묶음을 만든다.
판정 절차:
1. 먼저 anchor가 질문·답변·원인·결과·주장·근거·결론·사례 중 어떤 역할인지 판단한다.
2. anchor가 질문이면 직접 답변까지, 답변이면 질문 또는 답변의 대상을 특정하는 바로 앞 맥락까지 연결한다.
3. anchor가 원인이면 직접 결과까지, 결과이면 직접 원인까지 연결한다.
4. anchor가 주장이나 결론이면 납득에 필요한 최소 근거·전제까지, 사례이면 무엇을 설명하는 사례인지 알 수 있는 주장까지 연결한다.
5. anchor가 앞 문장의 지시어·생략된 주어·접속 표현을 이어받거나 뒤 섹션에서 의미가 마무리되면 해당 섹션을 연결한다.
완결성 기준: 시간순으로 이어 보았을 때 도입만 있고 결론이 없거나, 질문만 있고 답이 없거나, 결과만 있고 원인이 없어서는 안 된다. 일반적으로 anchor 외 1~3개가 자연스러운 결과이며, anchor 하나만으로 발화의 시작·맥락·핵심·마무리가 모두 독립적으로 성립할 때만 빈 배열을 반환한다.
필수성 검사: 후보를 빼면 대상이 불명확해지거나 논리적 비약·갑작스러운 시작·미완성된 결말이 생기는 경우에는 반드시 포함한다.
선택 금지: 같은 주제라는 이유, 흥미로운 보충, 유사 사례, 배경 상식, 간접 원인·결과, 반복, 핵심 이해에 필요하지 않은 일반적 도입·마무리, 있으면 좋은 설명은 모두 제외한다.
확산 금지: 선택한 섹션에 다시 필요한 원인·결과를 연쇄적으로 찾지 않는다. 오직 anchor와 직접 연결된 1단계 관계만 판정한다.
수량 제한: anchor 외 총 4개 이하, 시간상 anchor 이전은 최대 2개, 이후는 최대 2개다. 완결성을 만드는 범위에서 가장 적은 수를 선택한다.
ID 제한: 제공된 같은 챕터의 ID만 정확히 복사한다. anchor ID, 중복 ID, 입력에 없는 ID는 반환하지 않는다.
출력 계약: anchor 이전의 필수 ID는 before_ids에 시간순으로 최대 2개, 이후의 필수 ID는 after_ids에 시간순으로 최대 2개를 넣는다. 해당 방향에 필수 ID가 없으면 빈 배열을 쓴다.
출력 규칙: JSON 외 텍스트·설명·Markdown을 금지한다. {"before_ids":[string],"after_ids":[string]}만 반환한다.'''
TIMESTAMP_COMMENT_SCORE_SYSTEM = '''역할: 영상 타임스탬프 댓글의 편집 가치 판정기.
목표: 댓글 작성자가 특정 시각을 표시한 목적과 반응 강도를 댓글 원문만으로 판정하여, 해당 시각이 편집 하이라이트 후보로 갖는 가치를 0~1000 정수로 평가한다.

핵심 원칙:
1. 점수는 댓글의 문장 품질이나 길이가 아니라 타임스탬프가 가리키는 장면에 대한 시청자의 의도를 평가한다.
2. 입력에 없는 영상 내용, 감정, 사건, 인물 또는 맥락을 추측하지 않는다.
3. 타임스탬프가 여러 개 있어도 댓글 하나에는 하나의 점수를 부여한다. 클라이언트가 그 점수를 댓글에 포함된 모든 시각에 적용한다.
4. 좋아요 수, 작성자, 게시 시각 등 제공되지 않은 신호를 가정하지 않는다.
5. 모든 입력을 서로 비교하여 같은 종류의 반응에도 표현의 명확성·강도에 따라 일의 자리까지 구분한다. 습관적으로 둥근 점수만 반복하지 않는다.

목적별 점수 기준:
- 0~80: 챕터 목차, 구간 제목 목록, 진행 순서 정리, 단순 탐색 안내처럼 장면의 흥미와 무관한 표식. 명백한 챕터 표기는 0점에 가깝게 평가한다.
- 81~300: 오류 정정, 출처·링크 메모, 질문 위치 안내, 기술적 기록처럼 유용하지만 하이라이트 선호를 나타내지 않는 표식.
- 301~550: 타임스탬프만 있거나 의도가 불분명한 짧은 북마크, 별다른 평가가 없는 중립적 언급. 내용이 없다는 이유만으로 0점을 주지 않는다.
- 551~750: 재미, 공감, 유익함, 다시 보고 싶은 지점을 비교적 분명하게 표현한 반응.
- 751~900: 강한 웃음·놀라움·감탄·몰입, 핵심이라고 지목함, 반복 재생이나 적극 추천처럼 높은 관심을 명확히 나타낸 반응.
- 901~1000: 댓글만으로도 해당 순간이 매우 강한 화제성·감정 반응·결정적 하이라이트임이 예외적으로 명백한 경우. 과도하게 남발하지 않는다.

판정 순서:
1. 타임스탬프 주변 문구가 목차나 구간 제목인지 먼저 판정한다.
2. 그렇지 않으면 기록·질문·정정·중립 북마크인지 판정한다.
3. 감정이나 선호가 있으면 강도와 구체성을 판정한다.
4. 모호하면 중립 범위로 보수적으로 평가한다.
5. 입력 index를 그대로 유지하고 모든 항목을 정확히 한 번 반환한다.

입력 형식: {"comments":[{"index":정수,"text":"댓글 원문"}]}
출력 규칙: JSON 외 텍스트, Markdown, 코드펜스, 설명, 목적 분류, 이유를 반환하지 않는다.
출력 형식: {"items":[{"index":정수,"score":0~1000 정수}]}'''
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
            rule = "" if attempt == 0 else (
                "\n직전 응답 거부 사유: " + str(last_error)
                + " 설명하지 말고 이 사유를 고쳐 완결된 JSON 객체 하나만 반환하세요."
            )
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
        detail = f": {last_error}" if last_error is not None else ""
        raise LLMAnalysisError(f"LLM이 스무 번 연속 JSON 문법 또는 응답 계약을 지키지 않았습니다{detail}") from last_error
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
            return [{**section,"llm_score":round(float(scores[item["id"]])/1000,3)} for section,item in zip(chapter_sections,payload)]
        result=[]
        with ThreadPoolExecutor(max_workers=min(getattr(self, "_max_parallel_requests", 100),len(groups))) as executor:
            futures=[executor.submit(score, group) for group in groups.values()]
            for completed,future in enumerate(as_completed(futures),1):
                if cancel_callback: cancel_callback()
                result.extend(future.result())
                if progress_callback: progress_callback(completed,len(futures),"챕터별 섹션 중요도 평가")
        return sorted(result,key=lambda item:(str(item.get("chapter_id","")),float(item.get("start",0))))

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
                raise LLMAnalysisError("타임스탬프 댓글 점수 응답 객체 형식이 올바르지 않습니다.")
            items = raw.get("items")
            if not isinstance(items, list) or len(items) != len(rows):
                raise LLMAnalysisError("타임스탬프 댓글 점수 응답이 입력과 일치하지 않습니다.")
            if any(not isinstance(item, dict) or set(item) != {"index", "score"} for item in items):
                raise LLMAnalysisError("타임스탬프 댓글 점수 항목 필드가 응답 계약과 다릅니다.")
            scores = {item.get("index"): item.get("score") for item in items}
            expected = list(range(len(rows)))
            if len(scores) != len(expected) or set(scores) != set(expected):
                raise LLMAnalysisError("타임스탬프 댓글 인덱스가 입력과 일치하지 않습니다.")
            if any(type(scores[index]) is not int or not 0 <= scores[index] <= 1000 for index in expected):
                raise LLMAnalysisError("타임스탬프 댓글 점수 형식이 올바르지 않습니다.")
            return [
                {"index": index, "score": round(float(scores[index]) / 1000, 3)}
                for index in expected
            ]

        return self._request_json(
            TIMESTAMP_COMMENT_SCORE_SYSTEM,
            json.dumps({"comments": rows}, ensure_ascii=False, separators=(",", ":")),
            response_schema=COMMENT_SCORE_RESPONSE_SCHEMA,
            validator=validate,
            cancel_callback=cancel_callback,
        )

    def required_anchor_links(self, anchor_id: str, chapter_summary: str, sections: list[dict[str, Any]], *, cancel_callback: Callable[[], None] | None = None) -> list[str]:
        ids = [str(item["id"]) for item in sections]
        if anchor_id not in ids:
            raise LLMAnalysisError("관계 확장 앵커가 챕터에 없습니다.")
        anchor_index = ids.index(anchor_id)
        def validate(raw: Any) -> list[str]:
            if not isinstance(raw, dict) or set(raw) != {"before_ids", "after_ids"}:
                raise LLMAnalysisError("필수 관계 응답 형식이 올바르지 않습니다.")
            before_values, after_values = raw.get("before_ids"), raw.get("after_ids")
            if not isinstance(before_values, list) or not isinstance(after_values, list):
                raise LLMAnalysisError("필수 관계의 이전·이후 ID는 배열이어야 합니다.")
            values = [*before_values, *after_values]
            if len(before_values) > 2 or len(after_values) > 2 or any(not isinstance(value, str) for value in values) or len(values) != len(set(values)):
                raise LLMAnalysisError("필수 관계 ID 개수 또는 형식이 올바르지 않습니다.")
            if anchor_id in values or any(value not in ids for value in values):
                raise LLMAnalysisError("필수 관계 ID가 입력과 일치하지 않습니다.")
            if any(ids.index(value) >= anchor_index for value in before_values):
                raise LLMAnalysisError("before_ids에는 anchor 이전 ID만 사용할 수 있습니다.")
            if any(ids.index(value) <= anchor_index for value in after_values):
                raise LLMAnalysisError("after_ids에는 anchor 이후 ID만 사용할 수 있습니다.")
            return values
        payload = {"chapter_summary": chapter_summary, "anchor_id": anchor_id, "sections": sections}
        return self._request_json(ANCHOR_LINK_SYSTEM, json.dumps(payload, ensure_ascii=False, separators=(",", ":")), response_schema=ANCHOR_LINK_RESPONSE_SCHEMA, validator=validate, cancel_callback=cancel_callback)

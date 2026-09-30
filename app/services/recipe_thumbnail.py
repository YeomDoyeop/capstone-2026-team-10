"""로컬 JPEG 썸네일을 Gemini에 전달하고 실제 이미지 사용 여부를 확인한다."""
import base64

from app.services.llm_analysis_service import LLMAnalysisError, LLMAnalysisService
from app.services.llm_gateway import LLMGatewayError, safe_error_detail
from app.services.prompt_store import system_prompt


def describe_recipe_thumbnail(agent, directory, *, cancel_callback=None):
    paths = [directory / name for name in ("sddefault.jpg", "maxresdefault.jpg", "hqdefault.jpg", "mqdefault.jpg")]
    path = next((p for p in paths if p.is_file()), None)
    if path is None:
        return {"status": "unavailable", "reason": "저장된 썸네일이 없습니다."}
    try:
        with path.open("rb") as source:
            data = source.read(2_000_001)
        if len(data) > 2_000_000 or not data.startswith(b"\xff\xd8\xff"):
            return {"status": "unavailable", "reason": "2MB 이하 JPEG 썸네일만 지원합니다."}
        if cancel_callback:
            cancel_callback()
        vision = agent if agent.gateway.provider == "gemini" else LLMAnalysisService(
            provider="gemini", server_access_token=agent.gateway.server_access_token,
            checkpoint=getattr(agent, "_checkpoint", None),
            checkpoint_callback=getattr(agent, "_checkpoint_callback", None),
        )
        schema = {"type": "object", "additionalProperties": False,
                  "required": ["observations", "uncertainties"],
                  "properties": {key: {"type": "array", "items": {"type": "string"}}
                                 for key in ("observations", "uncertainties")}}

        def validate(raw):
            if not isinstance(raw, dict) or set(raw) != {"observations", "uncertainties"} or any(
                not isinstance(raw[key], list) or any(not isinstance(x, str) for x in raw[key]) for key in raw
            ):
                raise LLMAnalysisError("썸네일 관찰은 observations·uncertainties 문자열 배열이어야 합니다.")
            return raw

        result = vision._request_json(
            system_prompt("recipe_thumbnail"), "첨부한 요리 영상 썸네일을 관찰하세요.",
            image={"mime_type": "image/jpeg", "data": base64.b64encode(data).decode("ascii")},
            response_schema=schema, validator=validate, cancel_callback=cancel_callback,
        )
        return {"status": "analyzed", **result}
    except (OSError, LLMAnalysisError, LLMGatewayError) as exc:
        return {"status": "unavailable", "reason": safe_error_detail(exc)}

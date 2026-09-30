"""타임스탬프가 확인된 추임새만 제거하고 영상·자막에 같은 컷을 적용한다."""

from __future__ import annotations

import math
import re
from typing import Any


FILLERS = frozenset({"네", "녜", "넵", "어", "음", "아", "뭐"})
MAX_FILLER_SECONDS = 1.2
MAX_REMOVAL_RATIO = 0.20


def is_filler_text(text: str) -> bool:
    # 부분 문자열 검색은 '네트워크', '음성', '아마' 등을 손상시킨다.
    return text.strip().strip(".,!?…。，！？ ") in FILLERS


def timed_units(segment: dict[str, Any]) -> list[dict[str, Any]]:
    """모든 단어와 원문이 일치할 때만 단어 경계를 사용한다. 시간 추정은 하지 않는다."""
    text = str(segment.get("text") or "")
    words = segment.get("words")
    if not isinstance(words, list) or not words:
        return []
    result = []
    cursor = 0
    previous_end = float(segment["start"])
    for word in words:
        if not isinstance(word, dict):
            return []
        token = str(word.get("word") or "").strip()
        left = text.find(token, cursor) if token else -1
        if left < 0 or text[cursor:left].strip():
            return []
        try:
            start, end = float(word["start"]), float(word["end"])
        except (KeyError, TypeError, ValueError):
            return []
        if not (math.isfinite(start) and math.isfinite(end)
                and previous_end <= start < end <= float(segment["end"])):
            return []
        cursor = left + len(token)
        result.append({"start": start, "end": end, "text": token,
                       "char_start": left, "char_end": cursor})
        previous_end = end
    return result if not text[cursor:].strip() else []


def filler_candidates(segments: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    candidates = []
    missing_timing = 0
    for index, segment in enumerate(segments):
        if segment.get("timing_estimated"):
            missing_timing += 1
            continue
        text = str(segment.get("text") or "")
        units = timed_units(segment)
        if not units:
            if is_filler_text(text):
                units = [{"start": float(segment["start"]), "end": float(segment["end"]),
                          "text": text, "char_start": 0, "char_end": len(text)}]
            elif any(is_filler_text(token) for token in re.findall(r"[가-힣]+", text)):
                missing_timing += 1
        for unit in units:
            duration = unit["end"] - unit["start"]
            if not is_filler_text(unit["text"]) or not 0.04 <= duration <= MAX_FILLER_SECONDS:
                continue
            # 인접 자막과 음성이 겹치는 후보는 보존한다.
            if (index and float(segments[index - 1]["end"]) > unit["start"]) or (
                index + 1 < len(segments) and float(segments[index + 1]["start"]) < unit["end"]
            ):
                continue
            previous = " ".join(str(s.get("text") or "") for s in segments[max(0, index - 2):index])
            following = " ".join(str(s.get("text") or "") for s in segments[index + 1:index + 3])
            candidates.append({
                **unit, "id": len(candidates), "segment_index": index,
                "before": (previous + " " + text[:unit["char_start"]])[-400:],
                "after": (text[unit["char_end"]:] + " " + following)[:400],
            })
    return candidates, missing_timing


def apply_filler_cuts(segments, clips, cuts):
    """원본은 보존하고 선택 범위 내부의 승인된 컷만 복사본에 적용한다."""
    applicable = sorted((cut for cut in cuts if any(
        float(clip["start"]) <= cut["start"] < cut["end"] <= float(clip["end"])
        for clip in clips
    )), key=lambda cut: cut["start"])
    duration = sum(float(c["end"]) - float(c["start"]) for c in clips)
    removed = sum(c["end"] - c["start"] for c in applicable)
    summary = {"removed_count": 0, "removed_seconds": 0.0, "guard_triggered": False}
    if not applicable:
        return clips, segments, summary
    if removed > duration * MAX_REMOVAL_RATIO:
        return clips, segments, {**summary, "guard_triggered": True}
    edited_clips = []
    for clip in clips:
        cursor, end = float(clip["start"]), float(clip["end"])
        for cut in applicable:
            if not float(clip["start"]) <= cut["start"] < cut["end"] <= end:
                continue
            if cursor < cut["start"]:
                edited_clips.append({**clip, "start": cursor, "end": cut["start"], "filler_edited": True})
            cursor = cut["end"]
        if cursor < end:
            edited_clips.append({**clip, "start": cursor, "end": end, "filler_edited": True})
    cleaned = []
    for index, segment in enumerate(segments):
        text = str(segment.get("text") or "")
        for cut in sorted((c for c in applicable if c["segment_index"] == index),
                          key=lambda c: c["char_start"], reverse=True):
            text = text[:cut["char_start"]] + text[cut["char_end"]:]
        cleaned.append({**segment, "text": re.sub(r"\s+", " ", text).strip()})
    return edited_clips, cleaned, {**summary, "removed_count": len(applicable),
                                  "removed_seconds": round(removed, 3)}

"""구두점·의미 기준 문장 분리와 원본 자막 시간 매핑."""

from __future__ import annotations

import math
import re
from typing import Any, Callable

from app.services.filler_edit import timed_units


def sentence_spans(text: str) -> list[tuple[int, int]]:
    """마침표·물음표·느낌표를 보존하며 분리한다. 숫자 사이의 점은 제외한다."""
    spans = []
    cursor = 0
    for match in re.finditer(r"[.!?。！？]+[\"'”’)\]}]*", text):
        start, end = match.span()
        if (match.group() == "." and start > 0 and end < len(text)
                and text[start - 1].isdigit() and text[end].isdigit()):
            continue
        if text[cursor:end].strip():
            left = cursor + len(text[cursor:end]) - len(text[cursor:end].lstrip())
            spans.append((left, end))
        cursor = end
    if text[cursor:].strip():
        left = cursor + len(text[cursor:]) - len(text[cursor:].lstrip())
        spans.append((left, len(text.rstrip())))
    return spans


def _cut_time(cue: dict[str, Any], position: int) -> tuple[float, bool]:
    """단어 사이면 실측 시간 경계, 그 외 내부 경계는 문자 비율 추정값이다."""
    if position <= 0:
        return cue["start"], False
    if position >= len(cue["text"]):
        return cue["end"], False
    units = cue["units"]
    for previous, following in zip(units, units[1:]):
        if previous["char_end"] <= position <= following["char_start"]:
            return (previous["end"] + following["start"]) / 2, False
    for unit in units:
        if unit["char_start"] < position < unit["char_end"]:
            ratio = (position - unit["char_start"]) / (unit["char_end"] - unit["char_start"])
            return unit["start"] + ratio * (unit["end"] - unit["start"]), True
    # 공백 때문에 두 문장 사이에 임의의 영상 누락 구간이 생기지 않도록 한다.
    ratio = len(re.sub(r"\s", "", cue["text"][:position])) / len(re.sub(r"\s", "", cue["text"]))
    return cue["start"] + ratio * (cue["end"] - cue["start"]), True


def split_timed_sentences(
    segments: list[dict[str, Any]],
    *,
    semantic_splitter: Callable[[str], list[int]] | None = None,
) -> list[dict[str, Any]]:
    """자막 ID·줄 경계를 넘어 문장을 연결하고, 한 문장마다 새 ID를 부여한다."""
    cues = []
    chunks = []
    offset = 0
    for index, segment in enumerate(segments):
        text = str(segment.get("text") or "").strip()
        if not text:
            continue
        start, end = float(segment["start"]), float(segment["end"])
        if not (math.isfinite(start) and math.isfinite(end) and 0 <= start < end):
            raise ValueError("문장 분할에 사용할 자막 시간이 올바르지 않습니다.")
        if chunks:
            offset += 1
        cue = {"start": start, "end": end, "text": text, "words": segment.get("words")}
        cues.append({**cue, "units": timed_units(cue), "offset": offset,
                     "source_id": segment.get("id", index),
                     "estimated": bool(segment.get("timing_estimated"))})
        chunks.append(text)
        offset += len(text)
    combined = " ".join(chunks)
    spans = []
    for begin, finish in sentence_spans(combined):
        text = combined[begin:finish]
        # 구두점이 없는 잔여 구간과 구두점이 드문 긴 구간을 보완한다.
        terminal = re.search(r"[.!?。！？][\"'”’)\]}]*$", text)
        if semantic_splitter and (not terminal or len(text) > 120) and len(text.split()) > 1:
            ends = semantic_splitter(text)
            word_ends = {match.end() for match in re.finditer(r"\S+", text)}
            if (not ends or any(type(end) is not int or end not in word_ends for end in ends)
                    or ends != sorted(set(ends)) or ends[-1] != len(text)):
                raise ValueError("의미 기반 문장 경계가 원본 단어 범위를 완전히 덮지 않습니다.")
            cursor = 0
            for end in ends:
                left = cursor + len(text[cursor:end]) - len(text[cursor:end].lstrip())
                spans.append((begin + left, begin + end))
                cursor = end
        else:
            spans.append((begin, finish))
    result = []
    cue_index = 0
    for begin, finish in spans:
        while cue_index < len(cues) and cues[cue_index]["offset"] + len(cues[cue_index]["text"]) <= begin:
            cue_index += 1
        parts = []
        index = cue_index
        while index < len(cues) and cues[index]["offset"] < finish:
            cue = cues[index]
            left = max(0, begin - cue["offset"])
            right = min(len(cue["text"]), finish - cue["offset"])
            if left < right:
                parts.append((cue, left, right))
            index += 1
        first, left, _ = parts[0]
        last, _, right = parts[-1]
        start, estimated_start = _cut_time(first, left)
        end, estimated_end = _cut_time(last, right)
        if end <= start:
            raise ValueError("문장 시간 범위가 역전되었습니다. 원본 자막의 겹침을 확인하세요.")
        estimated = estimated_start or estimated_end or any(cue["estimated"] for cue, _, _ in parts)
        words = []
        # 추정 시간으로 단어 정렬을 위조하지 않는다. 추임새 컷은 실측 구간만 사용한다.
        if not estimated and all(cue["units"] for cue, _, _ in parts):
            words = [{"word": unit["text"], "start": unit["start"], "end": unit["end"]}
                     for cue, left, right in parts for unit in cue["units"]
                     if left <= unit["char_start"] and unit["char_end"] <= right]
        result.append({"id": len(result), "start": start, "end": end,
                       "text": combined[begin:finish], "words": words,
                       "timing_estimated": estimated,
                       "source_segment_ids": [cue["source_id"] for cue, _, _ in parts]})
    return result

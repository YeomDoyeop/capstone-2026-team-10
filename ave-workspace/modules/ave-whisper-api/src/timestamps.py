from typing import Any


def restore_original_segment_timestamps(
    segments: list[dict[str, Any]], duration: float, speed: float
) -> tuple[list[dict[str, Any]], float]:
    """배속된 오디오의 세그먼트 시각을 원본 시간축으로 되돌린다."""

    restored = [
        {
            "start": round(float(segment["start"]) * speed, 3),
            "end": round(float(segment["end"]) * speed, 3),
            "text": str(segment["text"]),
        }
        for segment in segments
    ]
    return restored, round(duration * speed, 3)

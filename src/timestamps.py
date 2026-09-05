from typing import Any


def restore_original_timestamps(segments: list[dict[str, Any]], duration: float, speed: float) -> tuple[list[dict[str, Any]], float]:
    """Convert timestamps from the speed-adjusted audio back to the original timeline."""
    restored_segments = [
        {
            # 외부 API나 입력 dict의 삽입 순서와 관계없이 저장·응답 JSON을
            # 사람이 읽기 쉬운 start → end → text 순서로 고정한다.
            "start": round(segment["start"] * speed, 3),
            "end": round(segment["end"] * speed, 3),
            "text": segment["text"],
        }
        for segment in segments
    ]
    return restored_segments, round(duration * speed, 3)

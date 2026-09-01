from typing import Any


def restore_original_timestamps(segments: list[dict[str, Any]], duration: float, speed: float) -> tuple[list[dict[str, Any]], float]:
    """Convert timestamps from the speed-adjusted audio back to the original timeline."""
    if speed == 1.0:
        return segments, duration

    restored_segments = [
        {
            **segment,
            "start": round(segment["start"] * speed, 3),
            "end": round(segment["end"] * speed, 3),
        }
        for segment in segments
    ]
    return restored_segments, round(duration * speed, 3)

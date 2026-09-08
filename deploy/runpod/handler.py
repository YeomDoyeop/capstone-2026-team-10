import json
import os
from typing import Any

import runpod

from src.transcription import transcribe


def handler(job: dict[str, Any]) -> dict[str, Any]:
    def report(progress: int, message: str) -> None:
        runpod.serverless.progress_update(
            job, {"progress": progress, "message": message}
        )

    return transcribe(job.get("input"), progress_callback=report)


if __name__ == "__main__":
    test_input = os.environ.get("RUNPOD_TEST_INPUT")
    if test_input:
        print(
            json.dumps(handler({"input": json.loads(test_input)}), ensure_ascii=False)
        )
    else:
        runpod.serverless.start({"handler": handler})

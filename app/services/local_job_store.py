"""완료된 편집 결과만 보관하는 로컬 SQLite 저장소."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class LocalJobStore:
    """실행 상태·전사문·LLM 응답은 저장하지 않는다."""

    def __init__(self, database_root: Path):
        self.path = database_root / "ave-client.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            # 개발용 이전 스키마에는 진행 상태·전사문·LLM 캐시가 포함되어
            # 있었으므로 호환 마이그레이션 없이 제거한다.
            connection.execute("DROP TABLE IF EXISTS edit_jobs")
            connection.execute("DROP TABLE IF EXISTS metadata_material_cache")
            connection.execute("DROP TABLE IF EXISTS llm_cache")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS completed_edits (
                    job_id TEXT PRIMARY KEY,
                    result_json TEXT NOT NULL,
                    completed_at TEXT NOT NULL
                )
                """)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _load(value: str | None) -> dict[str, Any] | None:
        try:
            parsed = json.loads(value or "")
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None

    def save_completed(
        self, job_id: str, result: dict[str, Any], *, owner_id: str | None = None
    ) -> None:
        completed_at = datetime.now(timezone.utc).isoformat()
        plan = (
            result.get("analysis_plan")
            if isinstance(result.get("analysis_plan"), dict)
            else {}
        )
        persisted_plan = {
            key: value
            for key, value in plan.items()
            if key
            in {
                "genre",
                "llm_provider",
                "transcription_source",
                "target_seconds",
                "chapters",
                "selected_segment_ids",
                "recommended_segment_ids",
                "clips",
            }
        }
        if isinstance(persisted_plan.get("clips"), list):
            persisted_plan["clips"] = [
                {
                    key: item[key]
                    for key in ("segment_id", "start", "end", "llm_score")
                    if key in item
                }
                for item in persisted_plan["clips"]
                if isinstance(item, dict)
            ]
        persisted = {
            "job_id": job_id,
            "owner_id": owner_id,
            "status": "completed",
            "phase": "completed",
            "progress": 100,
            "message": "AI 영상 편집이 완료되었습니다.",
            "result": {
                key: value
                for key, value in result.items()
                if key
                in {
                    "rendered_filename",
                    "rendered_video_path",
                    "vod_video_id",
                    "selected_segment_ids",
                    "selected_duration_seconds",
                }
            },
            "analysis_plan": persisted_plan,
            "completed_at": completed_at,
        }
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO completed_edits (job_id, result_json, completed_at)
                VALUES (?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    result_json = excluded.result_json,
                    completed_at = excluded.completed_at
                """,
                (
                    job_id,
                    json.dumps(persisted, ensure_ascii=False, separators=(",", ":")),
                    completed_at,
                ),
            )

    def get_completed(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT result_json FROM completed_edits WHERE job_id = ?", (job_id,)
            ).fetchone()
        return self._load(row["result_json"]) if row else None

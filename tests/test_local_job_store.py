from app.services.local_job_store import LocalJobStore


def test_local_job_store_keeps_only_completed_result(tmp_path):
    store = LocalJobStore(tmp_path)
    store.save_completed(
        "abcdefghijk.19d",
        {
            "rendered_filename": "abcdefghijk.19d.edited.mp4",
            "rendered_video_path": "/media/result.mp4",
            "candidates": [{"text": "저장하면 안 되는 분석 원문"}],
            "summary": {"summary": "저장하면 안 되는 요약"},
        },
    )

    completed = store.get_completed("abcdefghijk.19d")
    assert completed is not None
    assert completed["status"] == "completed"
    assert completed["result"]["rendered_filename"].endswith(".mp4")
    assert "candidates" not in completed["result"]
    assert "summary" not in completed["result"]


def test_local_job_store_removes_clip_transcript(tmp_path):
    store = LocalJobStore(tmp_path)
    store.save_completed(
        "abcdefghijk.19e",
        {
            "analysis_plan": {
                "clips": [
                    {
                        "segment_id": "chapter-00",
                        "start": 1,
                        "end": 2,
                        "text": "저장하면 안 되는 원문",
                    }
                ]
            }
        },
    )

    completed = store.get_completed("abcdefghijk.19e")
    assert completed is not None
    assert completed["analysis_plan"]["clips"] == [
        {"segment_id": "chapter-00", "start": 1, "end": 2}
    ]

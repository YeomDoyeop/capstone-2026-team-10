from app.services import whisper_api_service


class _Response:
    def raise_for_status(self):
        return None


def test_cancel_transcription_calls_runpod_cancel_endpoint(monkeypatch):
    calls = []
    monkeypatch.setattr(
        whisper_api_service,
        "_endpoint_and_headers",
        lambda: (
            "https://api.runpod.ai/v2/endpoint",
            {"Authorization": "Bearer secret"},
        ),
    )
    monkeypatch.setattr(
        whisper_api_service.requests,
        "post",
        lambda url, **kwargs: calls.append((url, kwargs)) or _Response(),
    )

    whisper_api_service.cancel_transcription("runpod-001")

    assert calls == [
        (
            "https://api.runpod.ai/v2/endpoint/cancel/runpod-001",
            {"headers": {"Authorization": "Bearer secret"}, "timeout": 30},
        )
    ]

import pytest
from pydantic import ValidationError

from app.schemas import RemoteTranscriptionRequest


def test_remote_transcription_speed_range_is_one_to_two():
    values = {"file_id": "a" * 32}

    assert RemoteTranscriptionRequest(**values, speed=1.0).speed == 1.0
    assert RemoteTranscriptionRequest(**values, speed=2.0).speed == 2.0
    assert RemoteTranscriptionRequest(**values, language=None).language is None
    with pytest.raises(ValidationError):
        RemoteTranscriptionRequest(**values, speed=0.9)
    with pytest.raises(ValidationError):
        RemoteTranscriptionRequest(**values, speed=2.1)

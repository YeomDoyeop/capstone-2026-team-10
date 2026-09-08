import importlib
import sys
from dataclasses import dataclass
from types import ModuleType, SimpleNamespace


@dataclass(frozen=True)
class _Options:
    initial_prompt: str | None = None
    hotwords: str | None = None


class _Model:
    options = _Options()

    def transcribe(self, _audio, **kwargs):
        assert kwargs["language"] == "ko"
        assert self.options.initial_prompt == "문장 부호"
        assert self.options.hotwords == "서울대학교"
        kwargs["progress_callback"](100)
        return {
            "language": "ko",
            "segments": [{"start": 0.0, "end": 1.0, "text": " 원문"}],
        }


def test_whisperx_result_is_forced_aligned_and_versioned(monkeypatch, tmp_path):
    monkeypatch.setenv("MODEL_CACHE_DIR", str(tmp_path / "models"))
    monkeypatch.setenv("ALIGN_MODEL_CACHE_DIR", str(tmp_path / "alignment"))
    fake = ModuleType("whisperx")
    fake.load_model = lambda *_args, **_kwargs: _Model()
    fake.load_audio = lambda _path: [0.0] * 16_000
    fake.load_align_model = lambda **_kwargs: (object(), {"language": "ko"})
    fake.align = lambda segments, *_args, **kwargs: (
        kwargs["progress_callback"](100)
        or {
            "segments": [
                {
                    "start": 0.12,
                    "end": 0.88,
                    "text": segments[0]["text"],
                    "words": [
                        {"start": 0.12, "end": 0.4, "word": " 원"},
                        {"start": 0.4, "end": 0.88, "word": "문"},
                    ],
                }
            ]
        }
    )
    audio = ModuleType("whisperx.audio")
    audio.SAMPLE_RATE = 16_000
    monkeypatch.setitem(sys.modules, "whisperx", fake)
    monkeypatch.setitem(sys.modules, "whisperx.audio", audio)
    sys.modules.pop("src.transcription", None)
    module = importlib.import_module("src.transcription")
    source = tmp_path / "audio.mp3"
    source.write_bytes(b"audio")
    monkeypatch.setattr(module, "download_audio", lambda *_args, **_kwargs: source)

    result = module.transcribe(
        {
            "audio_url": "https://example.com/audio.mp3",
            "language": "ko",
            "initial_prompt": "문장 부호",
            "hotwords": "서울대학교",
            "speed": 1.0,
        }
    )

    assert result["engine"] == "whisperx-aligned-word-v1"
    assert result["alignment"] == "ctc-forced-alignment-with-words"
    assert result["segments"] == [
        {
            "start": 0.12,
            "end": 0.88,
            "text": " 원문",
            "words": [
                {"start": 0.12, "end": 0.4, "word": " 원"},
                {"start": 0.4, "end": 0.88, "word": "문"},
            ],
        }
    ]

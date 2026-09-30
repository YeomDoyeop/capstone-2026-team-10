from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.toolchain import ToolchainError, ffmpeg, ytdlp
from app.services.ytdlp_binary import YoutubeDL


def test_toolchain_uses_configured_bin_directory(tmp_path, monkeypatch):
    for filename in ("yt-dlp.exe", "ffmpeg.exe", "ffprobe.exe"):
        (tmp_path / filename).write_bytes(b"binary")
    monkeypatch.setenv("AVE_BIN_DIR", str(tmp_path))

    assert ytdlp() == tmp_path / "yt-dlp.exe"
    assert ffmpeg() == tmp_path / "ffmpeg.exe"


def test_toolchain_does_not_fall_back_to_system_path(tmp_path, monkeypatch):
    monkeypatch.setenv("AVE_BIN_DIR", str(tmp_path))

    with pytest.raises(ToolchainError, match="yt-dlp.exe"):
        ytdlp()


def test_ytdlp_binary_converts_project_options_to_cli(monkeypatch, tmp_path):
    executable = tmp_path / "yt-dlp.exe"
    executable.write_bytes(b"binary")
    captured: list[str] = []
    monkeypatch.setattr("app.services.ytdlp_binary.ytdlp", lambda: executable)
    monkeypatch.setattr(
        "app.services.ytdlp_binary.subprocess.run",
        lambda arguments, **_kwargs: (
            captured.extend(arguments),
            SimpleNamespace(returncode=0, stdout='{"id":"video-id"}\n', stderr=""),
        )[1],
    )

    info = YoutubeDL(
        {
            "skip_download": True,
            "writeinfojson": True,
            "writecomments": False,
            "outtmpl": str(tmp_path / "%(id)s.%(ext)s"),
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": ["ko"],
            "subtitlesformat": "vtt",
            "extractor_args": {
                "youtube": {
                    "player_client": ["web_embedded"],
                    "lang": ["ko"],
                    "max_comments": ["0"],
                }
            },
        }
    ).extract_info("https://www.youtube.com/watch?v=dQw4w9WgXcQ", download=True)

    assert info["id"] == "video-id"
    assert "--write-info-json" in captured
    assert "--ignore-config" in captured
    assert "--no-write-comments" in captured
    assert "youtube:max_comments=0" in captured
    assert "--write-subs" in captured
    assert "--sub-langs" in captured
    assert "--extractor-args" in captured
    assert "youtube:lang=ko" in captured

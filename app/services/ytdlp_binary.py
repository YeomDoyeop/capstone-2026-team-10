"""yt-dlp 실행 파일을 기존 수집 코드가 사용할 수 있도록 감싼다."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from urllib.request import Request, urlopen

from app.services.toolchain import ToolchainError, ytdlp


class YtDlpBinaryError(RuntimeError):
    pass


class YoutubeDL:
    """이 프로젝트에서 쓰는 yt-dlp Python API 부분집합의 바이너리 구현."""

    def __init__(self, options: dict | None = None):
        self.options = options or {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def urlopen(self, url: str):
        headers = self.options.get("http_headers") or {}
        return urlopen(Request(url, headers=headers), timeout=30)

    def extract_info(self, url: str, download: bool = True) -> dict:
        arguments = [str(ytdlp()), "--ignore-config", "--no-warnings", "--no-playlist", "--print-json"]
        arguments.extend(self._option_arguments())
        if not download:
            arguments.append("--skip-download")
        arguments.append(url)
        try:
            completed = subprocess.run(
                arguments,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        except (OSError, ToolchainError) as exc:
            raise YtDlpBinaryError(str(exc)) from exc
        if completed.returncode != 0:
            raise YtDlpBinaryError((completed.stderr or completed.stdout).strip())
        for line in reversed(completed.stdout.splitlines()):
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                return value
        raise YtDlpBinaryError("yt-dlp가 메타데이터 JSON을 반환하지 않았습니다.")

    def _option_arguments(self) -> list[str]:
        value = self.options
        arguments: list[str] = []
        if value.get("skip_download"):
            arguments.append("--skip-download")
        if value.get("writeinfojson"):
            arguments.append("--write-info-json")
        if value.get("writecomments"):
            arguments.append("--write-comments")
        elif "writecomments" in value:
            arguments.append("--no-write-comments")
        if output := value.get("outtmpl"):
            arguments.extend(["--output", str(output)])
        if format_selector := value.get("format"):
            arguments.extend(["--format", str(format_selector)])
        if value.get("extractaudio"):
            arguments.append("--extract-audio")
        if audio_format := value.get("audioformat"):
            arguments.extend(["--audio-format", str(audio_format)])
        audio_quality = value.get("audioquality")
        if audio_quality is not None:
            arguments.extend(["--audio-quality", str(audio_quality)])
        if ffmpeg_location := value.get("ffmpeg_location"):
            arguments.extend(["--ffmpeg-location", str(ffmpeg_location)])
        if value.get("writesubtitles"):
            arguments.append("--write-subs")
        if value.get("writeautomaticsub"):
            arguments.append("--write-auto-subs")
        if languages := value.get("subtitleslangs"):
            arguments.extend(["--sub-langs", ",".join(str(language) for language in languages)])
        if subtitle_format := value.get("subtitlesformat"):
            arguments.extend(["--sub-format", str(subtitle_format)])
        if merged_format := value.get("merge_output_format"):
            arguments.extend(["--merge-output-format", str(merged_format)])
        for source, flag in (("retries", "--retries"), ("fragment_retries", "--fragment-retries"), ("file_access_retries", "--file-access-retries"), ("sleep_interval_requests", "--sleep-requests"), ("http_chunk_size", "--http-chunk-size")):
            if source in value:
                arguments.extend([flag, str(value[source])])
        if cookie_file := value.get("cookiefile"):
            arguments.extend(["--cookies", str(cookie_file)])
        if browser := value.get("cookiesfrombrowser"):
            arguments.extend(["--cookies-from-browser", str(browser[0])])
        for key, header_value in (value.get("http_headers") or {}).items():
            arguments.extend(["--add-header", f"{key}:{header_value}"])
        extractor_args = value.get("extractor_args") or {}
        youtube_args = extractor_args.get("youtube") or {}
        if clients := youtube_args.get("player_client"):
            arguments.extend(["--extractor-args", f"youtube:player_client={','.join(clients)}"])
        if languages := youtube_args.get("lang"):
            arguments.extend(["--extractor-args", f"youtube:lang={','.join(languages)}"])
        if comment_sort := youtube_args.get("comment_sort"):
            arguments.extend(["--extractor-args", f"youtube:comment_sort={','.join(comment_sort)}"])
        if max_comments := youtube_args.get("max_comments"):
            arguments.extend(["--extractor-args", f"youtube:max_comments={','.join(max_comments)}"])
        runtimes = value.get("js_runtimes") or {}
        if "node" in runtimes:
            arguments.extend(["--js-runtimes", "node"])
        return arguments

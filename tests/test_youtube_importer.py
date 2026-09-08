import json
from pathlib import Path

from app.services import youtube_importer
from app.services.youtube_importer import YouTubeImporter, format_info_json


def test_info_json_is_formatted_atomically_without_changing_values(tmp_path):
    info_path = tmp_path / "video.info.json"
    source = {"id": "video", "title": "한국어 제목", "nested": {"values": [1, 2]}}
    info_path.write_text(
        json.dumps(source, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )

    assert format_info_json(info_path) == source
    text = info_path.read_text(encoding="utf-8")
    assert text.endswith("\n")
    assert '\n  "title": "한국어 제목"' in text
    assert json.loads(text) == source
    assert not list(tmp_path.glob(".*.pending"))


def test_best_audio_uses_highest_quality_and_is_stored_in_yt_data(
    tmp_path, monkeypatch
):
    calls = []

    class AudioYoutubeDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, _url, download=True):
            calls.append(self.options)
            Path(self.options["outtmpl"].replace("%(ext)s", "mp3")).write_bytes(
                b"best audio"
            )
            return {"id": "abc123"}

    monkeypatch.setattr(youtube_importer, "YoutubeDL", AudioYoutubeDL)
    monkeypatch.setattr(youtube_importer, "ffmpeg", lambda: tmp_path / "ffmpeg.exe")

    path = YouTubeImporter(tmp_path).prepare_best_audio(
        "https://www.youtube.com/watch?v=abc123", "abc123"
    )

    assert path == tmp_path / "yt-data" / "abc123" / "abc123.mp3"
    assert path.read_bytes() == b"best audio"
    assert calls[0]["format"] == "bestaudio/best"
    assert calls[0]["audioformat"] == "mp3"
    assert calls[0]["audioquality"] == "0"


def test_import_reuses_existing_video_and_vtt_without_invoking_ytdlp(
    tmp_path, monkeypatch
):
    cache_dir = tmp_path / "yt-data" / "abc123"
    cache_dir.mkdir(parents=True)
    video = cache_dir / "sample-abc123.mp4"
    subtitle = cache_dir / "sample-abc123.ko.vtt"
    video.write_bytes(b"existing video")
    subtitle.write_text("WEBVTT\n", encoding="utf-8")
    (cache_dir / "abc123.info.json").write_text(
        json.dumps(
            {
                "source_url": "https://www.youtube.com/watch?v=abc123",
                "title": "Cached video",
                "duration": 60,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(youtube_importer, "YoutubeDL", None)

    result = YouTubeImporter(tmp_path).prepare_source_video(
        "https://www.youtube.com/watch?v=abc123", job_id="new-edit-job"
    )

    assert result["job_id"] == "new-edit-job"
    assert result["title"] == "Cached video"
    assert result["cache_hit"] is True
    assert Path(result["video_path"]).resolve() == video.resolve()
    assert [Path(path).resolve() for path in result["subtitle_files"]] == [
        subtitle.resolve()
    ]


def test_cached_source_video_is_reusable_without_vtt_for_whisper(tmp_path):
    cache_dir = tmp_path / "yt-data" / "abc123"
    cache_dir.mkdir(parents=True)
    video = cache_dir / "sample-abc123.mp4"
    video.write_bytes(b"existing video")
    (cache_dir / "abc123.info.json").write_text(
        json.dumps(
            {
                "source_url": "https://www.youtube.com/watch?v=abc123",
                "title": "Whisper source",
            }
        ),
        encoding="utf-8",
    )

    result = YouTubeImporter(tmp_path).find_complete_cached_import(
        "https://www.youtube.com/watch?v=abc123", "edit-job"
    )

    assert result is not None
    assert result["cache_hit"] is True
    assert result["subtitle_files"] == []
    assert Path(result["video_path"]).resolve() == video.resolve()


def test_import_reuses_phase_two_caption_when_downloading_the_source_video(
    tmp_path, monkeypatch
):
    cache_dir = tmp_path / "yt-data" / "abc123"
    captions_dir = cache_dir / "captions"
    captions_dir.mkdir(parents=True)
    (cache_dir / "abc123.info.json").write_text(
        json.dumps({"id": "abc123", "title": "Cached metadata", "duration": 60}),
        encoding="utf-8",
    )
    caption = captions_dir / "abc123.ko.vtt"
    caption.write_text("WEBVTT\n", encoding="utf-8")

    class VideoOnlyYoutubeDL:
        calls = []

        def __init__(self, options):
            self.options = options
            self.job_dir = Path(options["outtmpl"]).parent

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, url, download=True):
            self.__class__.calls.append(self.options)
            video = self.job_dir / "abc123.mp4"
            video.write_bytes(b"video")
            return {
                "id": "abc123",
                "title": "Downloaded video",
                "duration": 60,
                "webpage_url": url,
                "requested_downloads": [{"filepath": str(video)}],
            }

    monkeypatch.setattr(youtube_importer, "YoutubeDL", VideoOnlyYoutubeDL)
    monkeypatch.setattr(YouTubeImporter, "_has_ffmpeg", lambda self: True)

    result = YouTubeImporter(tmp_path).prepare_source_video(
        "https://www.youtube.com/watch?v=abc123"
    )

    assert VideoOnlyYoutubeDL.calls[0]["writesubtitles"] is False
    assert VideoOnlyYoutubeDL.calls[0]["writeautomaticsub"] is False
    assert VideoOnlyYoutubeDL.calls[0]["writeinfojson"] is False
    assert [Path(path).resolve() for path in result["subtitle_files"]] == [
        caption.resolve()
    ]


class SubtitleRateLimitedYoutubeDL:
    calls = []

    def __init__(self, options):
        self.options = options
        self.job_dir = Path(options["outtmpl"]).parent

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def extract_info(self, url, download=True):
        self.__class__.calls.append(self.options)

        if self.options["writesubtitles"]:
            raise RuntimeError(
                "ERROR: Unable to download video subtitles for 'en': "
                "HTTP Error 429: Too Many Requests"
            )

        video_path = self.job_dir / "sample-info-video.mp4"
        video_path.write_bytes(b"fake video")
        return {
            "title": "Sample info video",
            "duration": 90,
            "channel": "Sample Channel",
            "webpage_url": url,
            "requested_downloads": [{"filepath": str(video_path)}],
        }


def test_import_retries_without_subtitles_when_youtube_rate_limits_subtitles(
    tmp_path,
    monkeypatch,
):
    SubtitleRateLimitedYoutubeDL.calls = []
    monkeypatch.setattr(youtube_importer, "YoutubeDL", SubtitleRateLimitedYoutubeDL)
    monkeypatch.setattr(YouTubeImporter, "_has_ffmpeg", lambda self: True)
    importer = YouTubeImporter(tmp_path)

    result = importer.prepare_source_video("https://www.youtube.com/watch?v=abc123")

    assert len(SubtitleRateLimitedYoutubeDL.calls) == 2
    assert SubtitleRateLimitedYoutubeDL.calls[0]["writesubtitles"] is True
    assert SubtitleRateLimitedYoutubeDL.calls[1]["writesubtitles"] is False
    assert result["title"] == "Sample info video"
    assert result["subtitle_files"] == []
    assert result["warnings"] == [
        "Subtitle download was rate-limited by YouTube. "
        "The video was imported without subtitles."
    ]

    assert not (tmp_path / "yt-edit" / result["job_id"] / "import.json").exists()


class SingleFileYoutubeDL:
    calls = []

    def __init__(self, options):
        self.options = options
        self.job_dir = Path(options["outtmpl"]).parent

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def extract_info(self, url, download=True):
        self.__class__.calls.append(self.options)
        video_path = self.job_dir / "single-file-video.mp4"
        video_path.write_bytes(b"fake video")
        return {
            "title": "Single file video",
            "duration": 45,
            "webpage_url": url,
            "requested_downloads": [{"filepath": str(video_path)}],
        }


def test_import_uses_single_file_format_when_ffmpeg_is_missing(tmp_path, monkeypatch):
    SingleFileYoutubeDL.calls = []
    monkeypatch.setattr(youtube_importer, "YoutubeDL", SingleFileYoutubeDL)
    monkeypatch.setattr(
        YouTubeImporter, "_has_ffmpeg", lambda self: False, raising=False
    )
    importer = YouTubeImporter(tmp_path)

    result = importer.prepare_source_video("https://www.youtube.com/watch?v=abc123")

    assert SingleFileYoutubeDL.calls[0]["format"] == "best[ext=mp4]/best"
    assert "lang" not in SingleFileYoutubeDL.calls[0].get("extractor_args", {}).get(
        "youtube", {}
    )
    assert "merge_output_format" not in SingleFileYoutubeDL.calls[0]
    assert SingleFileYoutubeDL.calls[0]["writecomments"] is False
    assert SingleFileYoutubeDL.calls[0]["extractor_args"]["youtube"][
        "max_comments"
    ] == ["0"]
    assert result["title"] == "Single file video"
    assert result["warnings"] == [
        "ffmpeg is not installed. Downloaded a single-file video stream; "
        "quality may be lower."
    ]

    assert not (tmp_path / "yt-edit" / result["job_id"] / "import.json").exists()


class SeparateStreamsFallbackYoutubeDL(SingleFileYoutubeDL):
    calls = []

    def extract_info(self, url, download=True):
        self.__class__.calls.append(self.options)
        if self.options["format"] == "best[ext=mp4]/best":
            raise RuntimeError("ERROR: Requested format is not available")
        video_path = self.job_dir / "merged-video.mp4"
        video_path.write_bytes(b"fake video")
        return {
            "title": "Merged video",
            "duration": 45,
            "webpage_url": url,
            "requested_downloads": [{"filepath": str(video_path)}],
        }


def test_import_retries_with_separate_streams_when_combined_format_is_unavailable(
    tmp_path, monkeypatch
):
    SeparateStreamsFallbackYoutubeDL.calls = []
    monkeypatch.setattr(youtube_importer, "YoutubeDL", SeparateStreamsFallbackYoutubeDL)
    monkeypatch.setattr(
        YouTubeImporter, "_has_ffmpeg", lambda self: True, raising=False
    )
    importer = YouTubeImporter(tmp_path)

    result = importer.prepare_source_video("https://www.youtube.com/watch?v=abc123")

    assert [call["format"] for call in SeparateStreamsFallbackYoutubeDL.calls] == [
        "best[ext=mp4]/best",
        "bestvideo*+bestaudio/best",
    ]
    assert result["title"] == "Merged video"
    assert any("separate video and audio" in warning for warning in result["warnings"])


def test_import_record_only_formats_ytdlp_info_json(tmp_path):
    importer = YouTubeImporter(tmp_path)
    source_dir = tmp_path / "yt-data" / "abc123"
    source_dir.mkdir(parents=True)
    info_path = source_dir / "abc123.info.json"
    source_info = {"id": "abc123", "title": "yt-dlp source"}
    info_path.write_text(json.dumps(source_info), encoding="utf-8")

    returned_path = importer._write_metadata(
        source_dir,
        "edit-job",
        "https://www.youtube.com/watch?v=abc123",
        source_info,
        None,
        [],
        ["import warning"],
    )

    assert returned_path == info_path
    assert json.loads(info_path.read_text(encoding="utf-8")) == source_info
    assert '\n  "title"' in info_path.read_text(encoding="utf-8")
    assert json.loads(info_path.read_text(encoding="utf-8")) == source_info
    assert not (tmp_path / "yt-edit" / "edit-job" / "import.json").exists()


class ForbiddenThenFallbackYoutubeDL(SingleFileYoutubeDL):
    calls = []

    def extract_info(self, url, download=True):
        self.__class__.calls.append(self.options)
        client = (
            self.options.get("extractor_args", {})
            .get("youtube", {})
            .get("player_client", [None])[0]
        )
        if client == "web_embedded":
            raise RuntimeError(
                "ERROR: unable to download video data: HTTP Error 403: Forbidden"
            )
        video_path = self.job_dir / "fallback-video.mp4"
        video_path.write_bytes(b"fake video")
        return {
            "title": "Fallback video",
            "duration": 45,
            "webpage_url": url,
            "requested_downloads": [{"filepath": str(video_path)}],
        }


def test_import_retries_with_a_different_player_client_on_403(tmp_path, monkeypatch):
    ForbiddenThenFallbackYoutubeDL.calls = []
    monkeypatch.setenv("YTDLP_PLAYER_CLIENT", "web_embedded")
    monkeypatch.setattr(youtube_importer, "YoutubeDL", ForbiddenThenFallbackYoutubeDL)
    monkeypatch.setattr(
        YouTubeImporter, "_has_ffmpeg", lambda self: False, raising=False
    )
    importer = YouTubeImporter(tmp_path)

    result = importer.prepare_source_video("https://www.youtube.com/watch?v=abc123")

    clients = [
        options.get("extractor_args", {})
        .get("youtube", {})
        .get("player_client", [None])[0]
        for options in ForbiddenThenFallbackYoutubeDL.calls
    ]
    assert clients[:2] == ["web_embedded", None]
    assert result["title"] == "Fallback video"
    assert result["warnings"]


class CookieDatabaseThenSuccessYoutubeDL(SingleFileYoutubeDL):
    calls = []

    def extract_info(self, url, download=True):
        self.__class__.calls.append(self.options)
        if "cookiesfrombrowser" in self.options:
            raise RuntimeError("ERROR: Could not copy Chrome cookie database")
        video_path = self.job_dir / "cookie-fallback-video.mp4"
        video_path.write_bytes(b"fake video")
        return {
            "title": "Cookie fallback video",
            "duration": 45,
            "webpage_url": url,
            "requested_downloads": [{"filepath": str(video_path)}],
        }


def test_import_disables_browser_cookies_after_database_copy_failure(
    tmp_path, monkeypatch
):
    CookieDatabaseThenSuccessYoutubeDL.calls = []
    monkeypatch.setenv("YTDLP_COOKIES_FROM_BROWSER", "chrome")
    monkeypatch.delenv("YTDLP_COOKIEFILE", raising=False)
    monkeypatch.setattr(
        youtube_importer, "YoutubeDL", CookieDatabaseThenSuccessYoutubeDL
    )
    monkeypatch.setattr(
        YouTubeImporter, "_has_ffmpeg", lambda self: False, raising=False
    )
    importer = YouTubeImporter(tmp_path)

    result = importer.prepare_source_video("https://www.youtube.com/watch?v=abc123")

    assert len(CookieDatabaseThenSuccessYoutubeDL.calls) == 2
    assert "cookiesfrombrowser" in CookieDatabaseThenSuccessYoutubeDL.calls[0]
    assert "cookiesfrombrowser" not in CookieDatabaseThenSuccessYoutubeDL.calls[1]
    assert result["title"] == "Cookie fallback video"


class ForbiddenWithCookiesThenSuccessYoutubeDL(SingleFileYoutubeDL):
    calls = []

    def extract_info(self, url, download=True):
        self.__class__.calls.append(self.options)
        if "cookiesfrombrowser" in self.options:
            raise RuntimeError(
                "ERROR: unable to download video data: HTTP Error 403: Forbidden"
            )
        video_path = self.job_dir / "no-cookie-fallback-video.mp4"
        video_path.write_bytes(b"fake video")
        return {
            "title": "No cookie fallback video",
            "duration": 45,
            "webpage_url": url,
            "requested_downloads": [{"filepath": str(video_path)}],
        }


def test_import_retries_without_browser_cookies_after_403(tmp_path, monkeypatch):
    ForbiddenWithCookiesThenSuccessYoutubeDL.calls = []
    monkeypatch.setenv("YTDLP_COOKIES_FROM_BROWSER", "chrome")
    monkeypatch.delenv("YTDLP_COOKIEFILE", raising=False)
    monkeypatch.setattr(
        youtube_importer, "YoutubeDL", ForbiddenWithCookiesThenSuccessYoutubeDL
    )
    monkeypatch.setattr(
        YouTubeImporter, "_has_ffmpeg", lambda self: False, raising=False
    )
    importer = YouTubeImporter(tmp_path)

    result = importer.prepare_source_video("https://www.youtube.com/watch?v=abc123")

    assert len(ForbiddenWithCookiesThenSuccessYoutubeDL.calls) == 2
    assert "cookiesfrombrowser" in ForbiddenWithCookiesThenSuccessYoutubeDL.calls[0]
    assert "cookiesfrombrowser" not in ForbiddenWithCookiesThenSuccessYoutubeDL.calls[1]
    assert result["title"] == "No cookie fallback video"
    assert any("without browser cookies" in warning for warning in result["warnings"])

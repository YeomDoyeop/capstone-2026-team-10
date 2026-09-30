from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from app.config import get_media_root
from app.services.toolchain import ToolchainError, ffmpeg
from app.services.ytdlp_binary import YoutubeDL

YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov"}
SUBTITLE_EXTENSIONS = {".vtt", ".srt", ".json3"}

SUBTITLE_RATE_LIMIT_WARNING = (
    "Subtitle download was rate-limited by YouTube. "
    "The video was imported without subtitles."
)

FFMPEG_MISSING_WARNING = (
    "ffmpeg is not installed. Downloaded a single-file video stream; "
    "quality may be lower."
)

YTDLP_CLIENT_FALLBACK_WARNING = (
    "YouTube download required a fallback player client. "
    "Set YTDLP_COOKIES_FROM_BROWSER or YTDLP_COOKIEFILE if 403 continues."
)

YTDLP_COOKIE_WARNING = (
    "Browser cookie database could not be copied. "
    "Close the browser completely or use YTDLP_COOKIEFILE."
)

YTDLP_COOKIE_403_WARNING = (
    "YouTube rejected the cookie-authenticated stream with HTTP 403. "
    "Retried the download without browser cookies."
)

YTDLP_DRM_WARNING = (
    "YouTube marked this VOD as DRM protected. "
    "yt-dlp cannot download the protected stream; use an authorized local source video."
)

YTDLP_SEPARATE_STREAMS_WARNING = (
    "A combined video format was unavailable. Downloaded separate video and audio "
    "streams and merged them with ffmpeg."
)

YTDLP_STREAM_403_WARNING = (
    "YouTube metadata was readable, but the video stream returned HTTP 403. "
    "This is usually a player-client or PO Token restriction; use the web_embedded "
    "client and keep the bundled yt-dlp binary with Node/EJS installed."
)


def format_info_json(
    path: Path,
    fallback: dict | None = None,
    *,
    prefer_fallback: bool = False,
) -> dict:
    """yt-dlp info JSON을 내용 변경 없이 읽기 쉬운 형식으로 원자적 저장한다."""

    try:
        value = (
            fallback
            if prefer_fallback and fallback is not None
            else (
                json.loads(path.read_text(encoding="utf-8"))
                if path.is_file()
                else fallback
            )
        )
    except (OSError, json.JSONDecodeError) as exc:
        if fallback is None:
            raise YouTubeImportError("YouTube info JSON을 읽지 못했습니다.") from exc
        value = fallback
    if not isinstance(value, dict):
        raise YouTubeImportError("YouTube info JSON이 객체 형식이 아닙니다.")
    formatted = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    try:
        if path.is_file() and path.read_text(encoding="utf-8") == formatted:
            return value
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(f".{path.name}.{uuid4().hex}.pending")
    try:
        pending.write_text(formatted, encoding="utf-8")
        for attempt in range(5):
            try:
                os.replace(pending, path)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.05 * (attempt + 1))
    finally:
        pending.unlink(missing_ok=True)
    return value


class InvalidYouTubeURLError(ValueError):
    pass


class YouTubeImportError(RuntimeError):
    pass


def is_youtube_url(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    return parsed.scheme in {"http", "https"} and host in YOUTUBE_HOSTS


def relative_to_cwd(path: Path) -> str:
    try:
        return path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


class YouTubeImporter:
    def __init__(self, media_root: Path | None = None):
        self.media_root = media_root or get_media_root()

    async def import_video(self, url: str, job_id: str | None = None) -> dict:
        return await asyncio.to_thread(self.prepare_source_video, url, job_id)

    def prepare_source_video(
        self,
        url: str,
        job_id: str | None = None,
        *,
        include_subtitles: bool = True,
    ) -> dict:
        """원본 영상을 준비하거나 이미 확보한 ``yt-data`` 자료를 재사용한다."""
        if not is_youtube_url(url):
            raise InvalidYouTubeURLError("Only YouTube URLs are supported.")
        job_id = job_id or uuid4().hex
        cached = self.find_complete_cached_import(url, job_id)
        if cached is not None:
            return cached
        video_id = self._video_id_from_url(url)
        # A video ID is stable across edit jobs. Keeping newly imported assets
        # here makes later edit requests reuse both the source and its VTT.
        job_dir = self.media_root / "yt-data" / (video_id or job_id)
        job_dir.mkdir(parents=True, exist_ok=True)

        prefer_merged_formats = self._has_ffmpeg()
        warnings = []
        if not prefer_merged_formats:
            warnings.append(FFMPEG_MISSING_WARNING)

        existing_subtitles = [
            path
            for path in self._find_files(job_dir, SUBTITLE_EXTENSIONS, recursive=True)
            if path.suffix.lower() == ".vtt" and path.stat().st_size > 0
        ]
        info = self._download_with_fallbacks(
            url,
            job_dir,
            include_subtitles=include_subtitles and not bool(existing_subtitles),
            write_info_json=not self._has_cached_info(job_dir),
            prefer_merged_formats=prefer_merged_formats,
            warnings=warnings,
        )

        subtitle_files = self._find_files(job_dir, SUBTITLE_EXTENSIONS, recursive=True)
        video_file = self._find_video_file(job_dir, info)
        metadata_path = self._write_metadata(
            job_dir,
            job_id,
            url,
            info,
            video_file,
            subtitle_files,
            warnings,
        )

        return {
            "job_id": job_id,
            "source_url": url,
            "title": info.get("title"),
            "duration": info.get("duration"),
            "video_path": relative_to_cwd(video_file) if video_file else None,
            "subtitle_files": [relative_to_cwd(path) for path in subtitle_files],
            "metadata_path": relative_to_cwd(metadata_path),
            "warnings": warnings,
        }

    def prepare_best_audio(self, url: str, video_id: str) -> Path:
        """Download the best available audio and publish one reusable MP3."""

        if not is_youtube_url(url):
            raise InvalidYouTubeURLError("Only YouTube URLs are supported.")
        if not re.fullmatch(r"[A-Za-z0-9_-]{6,32}", video_id):
            raise YouTubeImportError("유효한 YouTube 영상 ID가 아닙니다.")
        data_dir = self.media_root / "yt-data" / video_id
        data_dir.mkdir(parents=True, exist_ok=True)
        destination = data_dir / f"{video_id}.mp3"
        if destination.is_file() and destination.stat().st_size > 0:
            return destination

        pending_stem = f".{video_id}.{uuid4().hex}.audio"
        last_error: Exception | None = None
        for player_client in self._player_clients():
            options = self._build_ydl_options(
                data_dir,
                include_subtitles=False,
                write_info_json=False,
                prefer_merged_formats=False,
                player_client=player_client,
            )
            options.update(
                {
                    "format": "bestaudio/best",
                    "outtmpl": str(data_dir / f"{pending_stem}.%(ext)s"),
                    "extractaudio": True,
                    "audioformat": "mp3",
                    "audioquality": "0",
                    "ffmpeg_location": str(ffmpeg()),
                }
            )
            try:
                self._download(url, options)
                pending = data_dir / f"{pending_stem}.mp3"
                if not pending.is_file() or pending.stat().st_size <= 0:
                    raise YouTubeImportError("최고 품질 MP3를 생성하지 못했습니다.")
                pending.replace(destination)
                return destination
            except Exception as exc:
                last_error = exc
        raise YouTubeImportError(
            self._clean_error(
                str(last_error or "최고 품질 오디오 다운로드에 실패했습니다.")
            )
        ) from last_error

    @staticmethod
    def _video_id_from_url(url: str) -> str | None:
        """Return a conservative YouTube ID without contacting YouTube."""

        parsed = urlparse(url)
        host = parsed.netloc.lower()
        if host == "youtu.be":
            candidate = parsed.path.strip("/").split("/", 1)[0]
        elif parsed.path.startswith("/watch"):
            candidate = parse_qs(parsed.query).get("v", [""])[0]
        elif parsed.path.startswith(("/shorts/", "/embed/", "/live/")):
            candidate = (
                parsed.path.strip("/").split("/", 1)[1]
                if "/" in parsed.path.strip("/")
                else ""
            )
        else:
            candidate = ""
        return (
            candidate if re.fullmatch(r"[A-Za-z0-9_-]{6,32}", candidate or "") else None
        )

    def find_complete_cached_import(self, url: str, job_id: str) -> dict | None:
        """Reuse an existing source video; transcript availability is checked separately."""

        video_id = self._video_id_from_url(url)
        if not video_id:
            return None
        root = self.media_root / "yt-data"
        if not root.exists():
            return None

        candidates = [root / video_id]
        candidates.extend(
            path for path in root.iterdir() if path.is_dir() and path not in candidates
        )
        matches: list[tuple[float, Path, Path, list[Path], dict]] = []
        for directory in candidates:
            info_paths = sorted(directory.glob("*.info.json"))
            metadata: dict = {}
            if info_paths:
                try:
                    stored_info = format_info_json(info_paths[0])
                    # ``yt-data`` contains the unmodified info JSON produced by
                    # yt-dlp.  Service state is deliberately kept in ``yt-edit``.
                    metadata = stored_info
                except (OSError, json.JSONDecodeError):
                    metadata = {}
            known_id = self._video_id_from_url(
                str(metadata.get("source_url") or metadata.get("webpage_url") or "")
            )
            # Legacy metadata may not contain a parseable URL, but yt-dlp file
            # names retain the video ID.
            if (
                known_id != video_id
                and video_id not in directory.name
                and not any(
                    video_id in path.name
                    for path in directory.iterdir()
                    if path.is_file()
                )
            ):
                continue
            subtitles = self._find_files(directory, SUBTITLE_EXTENSIONS, recursive=True)
            subtitles = [
                path
                for path in subtitles
                if path.suffix.lower() == ".vtt" and path.stat().st_size > 0
            ]
            video = self._find_video_file(directory, metadata)
            if video is None or not video.exists() or video.stat().st_size <= 0:
                continue
            newest = max(
                [video.stat().st_mtime, *(path.stat().st_mtime for path in subtitles)]
            )
            matches.append((newest, directory, video, subtitles, metadata))
        if not matches:
            return None

        _, directory, video, subtitles, metadata = max(
            matches, key=lambda item: item[0]
        )
        info_paths = sorted(directory.glob("*.info.json"))
        metadata_path = (
            info_paths[0] if info_paths else directory / f"{video_id}.info.json"
        )
        return {
            "job_id": job_id,
            "source_url": url,
            "title": metadata.get("title"),
            "duration": metadata.get("duration"),
            "video_path": relative_to_cwd(video),
            "subtitle_files": [relative_to_cwd(path) for path in subtitles],
            "metadata_path": (
                relative_to_cwd(metadata_path) if metadata_path.exists() else ""
            ),
            "warnings": ["Reused the existing source video for this YouTube video."],
            "cache_hit": True,
        }

    def _build_ydl_options(
        self,
        job_dir: Path,
        include_subtitles: bool,
        write_info_json: bool,
        prefer_merged_formats: bool,
        player_client: str | None = None,
        use_cookies: bool = True,
    ) -> dict:
        format_selector = os.getenv("YTDLP_FORMAT", "best[ext=mp4]/best").strip()
        options = {
            # Prefer a progressive MP4 stream. YouTube may expose separate
            # DASH streams whose URLs require a PO token and then return 403.
            # Users can opt back into a higher-quality selector via env.
            "format": format_selector or "best[ext=mp4]/best",
            "outtmpl": str(job_dir / "%(id)s.%(ext)s"),
            "writeautomaticsub": include_subtitles,
            "writesubtitles": include_subtitles,
            # The editor only consumes Korean captions. Requesting English as
            # well makes one rate-limited language fail the whole extraction.
            "subtitleslangs": ["ko"] if include_subtitles else [],
            "subtitlesformat": "vtt",
            "writeinfojson": write_info_json,
            # yt-dlp may fetch comments it considers quick even when comment
            # writing is disabled. Every non-comment extraction must set the
            # YouTube extractor limit to zero as well.
            "writecomments": False,
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "retries": 3,
            "fragment_retries": 3,
            "file_access_retries": 3,
            "sleep_interval_requests": 1,
            "http_chunk_size": 10 * 1024 * 1024,
            # Current YouTube extraction requires an external JS runtime and
            # the EJS challenge scripts. Node is installed with the project
            # environment; explicitly enable it for the Python API just as
            # the CLI would use --js-runtimes node.
            "js_runtimes": {"node": {}},
        }
        if prefer_merged_formats:
            options["merge_output_format"] = "mp4"
        youtube_args = {"max_comments": ["0"]}
        if player_client:
            youtube_args["player_client"] = [player_client]
        options["extractor_args"] = {"youtube": youtube_args}

        cookie_file = os.getenv("YTDLP_COOKIEFILE", "").strip()
        if use_cookies and cookie_file:
            options["cookiefile"] = cookie_file
        browser = os.getenv("YTDLP_COOKIES_FROM_BROWSER", "").strip()
        if use_cookies and browser and not cookie_file:
            # yt-dlp expects (browser, profile, keyring, container).
            options["cookiesfrombrowser"] = (browser, None, None, None)
        user_agent = os.getenv("YTDLP_USER_AGENT", "").strip()
        if user_agent:
            options["http_headers"] = {"User-Agent": user_agent}
        return options

    def _player_clients(self) -> list[str | None]:
        configured = os.getenv("YTDLP_PLAYER_CLIENT", "web_embedded").strip()
        # The tv client can report ordinary YouTube videos as DRM protected.
        # Keep the fallback limited to the embedded client and yt-dlp default.
        clients: list[str | None] = [configured or "web_embedded", None]
        return list(dict.fromkeys(clients))

    def _download_with_fallbacks(
        self,
        url: str,
        job_dir: Path,
        *,
        include_subtitles: bool,
        write_info_json: bool,
        prefer_merged_formats: bool,
        warnings: list[str],
    ) -> dict:
        last_error: Exception | None = None
        clients = self._player_clients()
        cookies_disabled = False
        for index, client in enumerate(clients):
            options = self._build_ydl_options(
                job_dir,
                include_subtitles=include_subtitles,
                write_info_json=write_info_json,
                prefer_merged_formats=prefer_merged_formats,
                player_client=client,
                use_cookies=not cookies_disabled,
            )
            try:
                return self._download(url, options)
            except Exception as exc:  # yt-dlp exposes several custom errors.
                last_error = exc
                if self._is_format_unavailable_error(exc) and prefer_merged_formats:
                    fallback_options = self._build_ydl_options(
                        job_dir,
                        include_subtitles=include_subtitles,
                        write_info_json=write_info_json,
                        prefer_merged_formats=True,
                        player_client=client,
                        use_cookies=not cookies_disabled,
                    )
                    fallback_options["format"] = "bestvideo*+bestaudio/best"
                    try:
                        result = self._download(url, fallback_options)
                        warnings.append(YTDLP_SEPARATE_STREAMS_WARNING)
                        return result
                    except Exception as fallback_exc:
                        last_error = fallback_exc
                if self._is_drm_error(exc) and index < len(clients) - 1:
                    continue
                if self._is_cookie_database_error(exc) and not cookies_disabled:
                    warnings.append(YTDLP_COOKIE_WARNING)
                    cookies_disabled = True
                    continue
                # A valid browser cookie can still make YouTube select a
                # protected/expired format. Retry the same client without
                # cookies before switching to another player client.
                if self._is_forbidden_error(exc) and not cookies_disabled:
                    warnings.append(YTDLP_COOKIE_403_WARNING)
                    cookies_disabled = True
                    continue
                if self._is_subtitle_rate_limit_error(exc):
                    warnings.append(SUBTITLE_RATE_LIMIT_WARNING)
                    fallback_options = self._build_ydl_options(
                        job_dir,
                        include_subtitles=False,
                        write_info_json=write_info_json,
                        prefer_merged_formats=prefer_merged_formats,
                        player_client=client,
                        use_cookies=not cookies_disabled,
                    )
                    try:
                        return self._download(url, fallback_options)
                    except Exception as fallback_exc:
                        last_error = fallback_exc
                if self._is_forbidden_error(last_error) and index < len(clients) - 1:
                    warnings.append(YTDLP_CLIENT_FALLBACK_WARNING)
                    continue
                if index < len(clients) - 1:
                    continue
        if self._is_drm_error(last_error):
            message = YTDLP_DRM_WARNING
        elif self._is_forbidden_error(last_error):
            message = YTDLP_STREAM_403_WARNING
        else:
            message = self._clean_error(str(last_error or "unknown yt-dlp error"))
        raise YouTubeImportError(message) from last_error

    def _download(self, url: str, ydl_options: dict) -> dict:
        with YoutubeDL(ydl_options) as downloader:
            return downloader.extract_info(url, download=True)

    def _has_ffmpeg(self) -> bool:
        try:
            ffmpeg()
        except ToolchainError:
            return False
        return True

    def _is_subtitle_rate_limit_error(self, exc: Exception) -> bool:
        message = str(exc)
        return "Unable to download video subtitles" in message and (
            "429" in message or "Too Many Requests" in message
        )

    @staticmethod
    def _is_format_unavailable_error(exc: Exception) -> bool:
        return "Requested format is not available" in str(exc)

    def _is_forbidden_error(self, exc: Exception | None) -> bool:
        return "403" in str(exc or "") or "Forbidden" in str(exc or "")

    def _is_cookie_database_error(self, exc: Exception | None) -> bool:
        message = str(exc or "").lower()
        return (
            "could not copy chrome cookie database" in message
            or "could not copy" in message
            and "cookie database" in message
        )

    def _is_drm_error(self, exc: Exception | None) -> bool:
        return "drm protected" in str(exc or "").lower()

    def _clean_error(self, message: str) -> str:
        return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", message).strip()

    def _write_metadata(
        self,
        job_dir: Path,
        job_id: str,
        url: str,
        info: dict,
        video_file: Path | None,
        subtitle_files: list[Path],
        warnings: list[str],
    ) -> Path:
        info_path = job_dir / f"{info.get('id') or job_dir.name}.info.json"
        format_info_json(info_path, info)
        return info_path

    def _find_video_file(self, job_dir: Path, info: dict) -> Path | None:
        for item in info.get("requested_downloads") or []:
            filepath = item.get("filepath")
            if filepath:
                candidate = Path(filepath)
                if candidate.exists():
                    return candidate

        files = self._find_files(job_dir, VIDEO_EXTENSIONS)
        if not files:
            return None
        return max(files, key=lambda path: path.stat().st_size)

    @staticmethod
    def _has_cached_info(job_dir: Path) -> bool:
        """Use phase-one info only when it is a readable yt-dlp JSON object."""

        for path in job_dir.glob("*.info.json"):
            try:
                if isinstance(format_info_json(path), dict):
                    return True
            except (OSError, YouTubeImportError):
                continue
        return False

    def _find_files(
        self,
        directory: Path,
        extensions: set[str],
        *,
        recursive: bool = False,
    ) -> list[Path]:
        paths = directory.rglob("*") if recursive else directory.glob("*")
        return sorted(
            path
            for path in paths
            if path.is_file() and path.suffix.lower() in extensions
        )

"""YouTube live metadata, captions, and chat helpers."""

from __future__ import annotations

import html
import hashlib
import json
import os
import re
from typing import Callable
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse
from uuid import uuid4

from app.config import get_media_root
from app.services.ytdlp_binary import YoutubeDL
from app.services.youtube_importer import (
    YouTubeImporter,
    YouTubeImportError,
    format_info_json,
)

VIDEO_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{11}$")
THUMBNAIL_FILENAMES = {"sddefault.jpg", "sd1.jpg", "sd2.jpg", "sd3.jpg"}
METADATA_PREFERRED_LANGUAGE = "ko"
METADATA_POLICY_VERSION = 4


class LiveYouTubeError(RuntimeError):
    pass


def _find_description_chapter_renderers(value: object) -> list[dict]:
    """YouTube 초기 데이터에서 플레이어의 실제 챕터 렌더러를 찾는다."""

    if isinstance(value, dict):
        player_bar = value.get("multiMarkersPlayerBarRenderer")
        if isinstance(player_bar, dict):
            for marker_map in player_bar.get("markersMap") or []:
                if (
                    not isinstance(marker_map, dict)
                    or marker_map.get("key") != "DESCRIPTION_CHAPTERS"
                ):
                    continue
                chapters = (marker_map.get("value") or {}).get("chapters") or []
                return [
                    chapter["chapterRenderer"]
                    for chapter in chapters
                    if isinstance(chapter, dict)
                    and isinstance(chapter.get("chapterRenderer"), dict)
                ]
        for child in value.values():
            result = _find_description_chapter_renderers(child)
            if result:
                return result
    elif isinstance(value, list):
        for child in value:
            result = _find_description_chapter_renderers(child)
            if result:
                return result
    return []


def _localized_youtube_chapters(downloader, url: str, duration: object) -> list[dict]:
    """한국어 YouTube 페이지가 제공하는 챕터 제목과 시각을 직접 읽는다."""

    if not isinstance(duration, (int, float)) or isinstance(duration, bool):
        return []
    parsed_url = urlparse(url)
    query = parse_qs(parsed_url.query)
    query["hl"] = [METADATA_PREFERRED_LANGUAGE]
    query["gl"] = ["KR"]
    localized_url = urlunparse(parsed_url._replace(query=urlencode(query, doseq=True)))
    response = downloader.urlopen(localized_url)
    try:
        page = response.read().decode("utf-8", errors="replace")
    finally:
        response.close()
    marker = "var ytInitialData = "
    start = page.find(marker)
    if start < 0:
        return []
    initial_data, _ = json.JSONDecoder().raw_decode(page, start + len(marker))
    renderers = _find_description_chapter_renderers(initial_data)
    parsed: list[tuple[float, str]] = []
    for renderer in renderers:
        title = (renderer.get("title") or {}).get("simpleText")
        start_millis = renderer.get("timeRangeStartMillis")
        if (
            not isinstance(title, str)
            or not title.strip()
            or not isinstance(start_millis, (int, float))
        ):
            continue
        parsed.append((float(start_millis) / 1000, title.strip()))
    if not parsed:
        return []
    return [
        {
            "start_time": start,
            "end_time": (
                parsed[index + 1][0] if index + 1 < len(parsed) else float(duration)
            ),
            "title": title,
        }
        for index, (start, title) in enumerate(parsed)
    ]


def _download_thumbnail_list(
    downloader,
    thumbnails: list[dict],
    main_thumbnail: str | None,
    output_dir: Path,
    video_id: str,
) -> list[dict]:
    """Save every thumbnail entry reported by yt-dlp, without deduplication."""

    thumbnail_dir = output_dir / "thumbnails"
    thumbnail_dir.mkdir(parents=True, exist_ok=True)
    saved: list[dict] = []
    for index, thumbnail in enumerate(thumbnails, start=1):
        if not isinstance(thumbnail, dict) or not isinstance(thumbnail.get("url"), str):
            continue
        # Store the filename yt-dlp exposes in the thumbnail URL unchanged.
        filename = Path(urlparse(thumbnail["url"]).path).name
        if filename not in THUMBNAIL_FILENAMES:
            continue
        is_primary = filename == "sddefault.jpg"
        path = thumbnail_dir / filename
        try:
            if not path.exists():
                response = downloader.urlopen(thumbnail["url"])
                try:
                    content = response.read()
                finally:
                    response.close()
                if not content:
                    continue
                path.write_bytes(content)
        except Exception:
            # An unavailable secondary thumbnail must not make metadata
            # inspection fail; the original URL remains in info.json.
            continue
        saved.append(
            {
                "id": str(index),
                "url": f"/api/youtube/thumbnail/{video_id}/{filename}",
                "source_url": thumbnail["url"],
                "width": thumbnail.get("width"),
                "height": thumbnail.get("height"),
                "is_primary": is_primary,
            }
        )
    if saved and not any(item["is_primary"] for item in saved):
        saved[0]["is_primary"] = True
    return saved


def _cached_thumbnail_files(
    output_dir: Path, video_id: str, main_thumbnail: str | None
) -> list[dict]:
    """Expose previously saved thumbnail assets without contacting YouTube."""

    thumbnail_dir = output_dir / "thumbnails"
    if not thumbnail_dir.exists():
        return []
    files: list[dict] = []
    for path in sorted(thumbnail_dir.iterdir()):
        if not path.is_file():
            continue
        if path.name not in THUMBNAIL_FILENAMES:
            continue
        files.append(
            {
                "id": path.stem,
                "url": f"/api/youtube/thumbnail/{video_id}/{path.name}",
                "is_primary": path.name == "sddefault.jpg",
            }
        )
    if files and not any(item["is_primary"] for item in files):
        files[0]["is_primary"] = True
    return files


def _chat_text(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if isinstance(value.get("simpleText"), str):
            return value["simpleText"]
        runs = value.get("runs")
        if isinstance(runs, list):
            return "".join(
                str(run.get("text", "")) for run in runs if isinstance(run, dict)
            )
    return ""


def _normalize_ytdlp_replay_action(action: dict) -> list[dict]:
    replay = action.get("replayChatItemAction") if isinstance(action, dict) else None
    if not isinstance(replay, dict):
        return []
    try:
        elapsed_seconds = float(replay.get("videoOffsetTimeMsec")) / 1000
    except (TypeError, ValueError):
        return []
    messages: list[dict] = []
    for nested in replay.get("actions", []):
        item = (
            nested.get("addChatItemAction", {}).get("item", {})
            if isinstance(nested, dict)
            else {}
        )
        if not isinstance(item, dict):
            continue
        renderer_name, renderer = next(
            (
                (name, value)
                for name, value in item.items()
                if name.endswith("Renderer") and isinstance(value, dict)
            ),
            (None, None),
        )
        if renderer is None:
            continue
        message = _chat_text(renderer.get("message")) or _chat_text(
            renderer.get("headerSubtext")
        )
        messages.append(
            {
                "id": renderer.get("id"),
                "author": _chat_text(renderer.get("authorName")) or None,
                "author_id": renderer.get("authorExternalChannelId"),
                "elapsed_seconds": elapsed_seconds,
                "message": message,
                "type": renderer_name,
                "super_chat": _chat_text(renderer.get("purchaseAmountText")) or None,
            }
        )
    return messages


def _vtt_timestamp_seconds(value: str) -> float:
    """Convert a WebVTT timestamp to seconds; malformed values sort last."""

    try:
        hours, minutes, seconds = value.replace(",", ".").split(":")
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except (TypeError, ValueError):
        return float("inf")


def _clean_vtt_text(text_lines: list[str]) -> str:
    """Remove WebVTT timing/style markup while preserving readable cue text."""

    text = " ".join(text_lines)
    text = re.sub(r"<[^>]*>", "", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _vtt_timed_words(text_lines: list[str], start: str, end: str) -> list[dict]:
    """명시적인 인라인 시각 사이의 텍스트만 보존한다. 단어 시간을 비례 추정하지 않는다."""
    words = []
    for line in text_lines:
        parts = re.split(r"<(\d{2}:\d{2}:\d{2}\.\d{3})>", line)
        if len(parts) < 3:
            continue  # 롤링 자막의 이전 줄에는 현재 발화 시각을 부여하지 않는다.
        current = _vtt_timestamp_seconds(start)
        for index in range(0, len(parts), 2):
            boundary = _vtt_timestamp_seconds(parts[index + 1] if index + 1 < len(parts) else end)
            text = _clean_vtt_text([parts[index]])
            if text and current < boundary:
                words.append({"word": text, "start": current, "end": boundary})
            current = boundary
    return words


def _parse_vtt_rows(content: str, filename: str) -> list[dict]:
    rows: list[dict] = []
    lines = content.replace("\r", "").split("\n")
    index = 0
    while index < len(lines):
        if "-->" not in lines[index]:
            index += 1
            continue
        start, end = (
            part.strip().split(" ", 1)[0] for part in lines[index].split("-->", 1)
        )
        index += 1
        text_lines = []
        while index < len(lines) and "-->" not in lines[index]:
            if lines[index].strip():
                text_lines.append(lines[index])
            elif text_lines:
                break
            index += 1
        rows.append(
            {
                "filename": filename,
                "start": start,
                "end": end,
                "duration_seconds": _vtt_timestamp_seconds(end)
                - _vtt_timestamp_seconds(start),
                "text": _clean_vtt_text(text_lines),
                "words": _vtt_timed_words(text_lines, start, end),
            }
        )
    return rows


def _rolling_caption_rows(rows: list[dict]) -> list[dict]:
    """Keep completed fragments from YouTube's rolling-caption WebVTT cues.

    Automatic captions commonly contain a long, progressively rewritten cue and
    a near-zero-length cue with just the newly completed fragment. The latter
    avoids showing the same growing sentence over and over in the inspector.
    """

    completed = [
        (index, row)
        for index, row in enumerate(rows)
        if 0 <= row.get("duration_seconds", 1) <= 0.05 and row.get("text")
    ]
    if completed:
        restored = []
        for source_index, row in completed:
            spoken_cue = next(
                (
                    candidate
                    for candidate in reversed(rows[:source_index])
                    if candidate.get("duration_seconds", 0) > 0.05
                    and candidate.get("text")
                ),
                None,
            )
            start = spoken_cue["start"] if spoken_cue else row["start"]
            end = row["start"]
            restored.append(
                {
                    **row,
                    "start": start,
                    "end": end,
                    "duration_seconds": _vtt_timestamp_seconds(end)
                    - _vtt_timestamp_seconds(start),
                    "words": (spoken_cue or {}).get("words", []),
                }
            )
        source = restored
    else:
        source = rows
    cleaned: list[dict] = []
    previous_text = ""
    for row in source:
        text = str(row.get("text") or "")
        if not text or text == previous_text:
            continue
        cleaned.append(row)
        previous_text = text
    return cleaned


_CAPTION_NON_SPEECH_LABELS = {
    "음악",
    "노래",
    "박수",
    "웃음",
    "환호",
    "탄식",
    "비명",
    "효과음",
    "잡음",
    "music",
    "applause",
    "laughter",
    "laughs",
    "cheering",
    "sighs",
    "screaming",
    "sound effect",
    "noise",
    "silence",
}


def _clean_caption_rows(rows: list[dict]) -> list[dict]:
    """캡션의 비언어 단독 큐를 버리고 발화자 전환 기호만 제거한다."""

    cleaned: list[dict] = []
    for row in rows:
        text = str(row.get("text") or "").strip()
        label_match = re.fullmatch(r"[\[\(（【]\s*([^\]\)）】]+?)\s*[\]\)）】]", text)
        if (
            label_match
            and label_match.group(1).strip().casefold() in _CAPTION_NON_SPEECH_LABELS
        ):
            continue
        if re.fullmatch(r"[♪♫♬♩\s]+", text):
            continue
        text = re.sub(r"^\s*(?:(?:>{1,2}|＞{1,2}|≫)\s*)+", "", text).strip()
        if text:
            cleaned.append({**row, "text": text})
    return cleaned


def _write_display_vtt(path: Path, rows: list[dict]) -> None:
    """2단계에서 확정한 표시용 WebVTT를 이후 단계가 그대로 사용하게 한다."""
    lines = ["WEBVTT", ""]
    for row in rows:
        if not row.get("text"):
            continue
        lines.extend([f"{row['start']} --> {row['end']}", str(row["text"]), ""])
    path.write_text("\n".join(lines), encoding="utf-8")


def _as_sort_number(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _live_chat_jsonl_files(output_dir: Path) -> list[Path]:
    """Use the JSONL extension for yt-dlp's line-delimited chat replay."""

    for json_path in output_dir.glob("*.live_chat.json"):
        jsonl_path = json_path.with_suffix(".jsonl")
        if not jsonl_path.exists():
            json_path.rename(jsonl_path)
    return sorted(
        output_dir.glob("*.live_chat.jsonl"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )


def _metadata_edit_dir(video_id: str) -> Path:
    """원본 yt-dlp 산출물에서 파생한 2단계 작업 파일의 고정 위치."""
    path = get_media_root() / "yt-edit" / video_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def prepared_metadata_paths(video_id: str) -> dict[str, Path]:
    """3단계가 재사용할 2단계 파생 자료의 공개 경계다.

    이 함수는 파일을 만들거나 원격에 요청하지 않는다. 호출자는 반환된
    경로만 읽어야 하며, 자료가 없으면 먼저 2단계를 실행해야 한다.
    """

    directory = get_media_root() / "yt-edit" / video_id
    return {
        "directory": directory,
        "chat_times": directory / f"{video_id}.chat-times.json",
        "whisper_audio": directory / f"{video_id}.whisper.mp3",
    }


def load_prepared_transcript(
    video_id: str, source_kind: str, language: str
) -> list[dict]:
    """선택 언어의 2단계 스크립트만 다시 파싱 없이 읽는다."""

    if source_kind not in {"subtitles", "captions"}:
        raise LiveYouTubeError("지원하지 않는 스크립트 소스입니다.")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,39}", language):
        raise LiveYouTubeError("선택한 스크립트 언어가 올바르지 않습니다.")
    parsed_path = (
        prepared_metadata_paths(video_id)["directory"]
        / f"{video_id}.{language}.{source_kind}-transcript.json"
    )
    if not parsed_path.is_file():
        raise LiveYouTubeError(
            f"2단계에서 준비한 {language} {source_kind} 파싱 파일을 찾지 못했습니다."
        )
    try:
        payload = json.loads(parsed_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LiveYouTubeError("2단계 스크립트 파일을 읽을 수 없습니다.") from exc
    if isinstance(payload, dict):
        payload = payload.get("segments")
    if not isinstance(payload, list):
        raise LiveYouTubeError("2단계 스크립트 파일 형식이 올바르지 않습니다.")
    return [row for row in payload if isinstance(row, dict)]


def extract_video_id(url: str) -> str:
    parsed = urlparse(url.strip())
    host = parsed.netloc.lower().split(":", 1)[0]
    video_id = ""

    if host == "youtu.be":
        video_id = parsed.path.strip("/").split("/")[0]
    elif host.endswith("youtube.com"):
        query_id = parse_qs(parsed.query).get("v", [""])[0]
        if parsed.path == "/watch":
            video_id = query_id
        elif parsed.path.startswith("/live/"):
            video_id = parsed.path.split("/", 2)[2].split("/", 1)[0]
        elif parsed.path.startswith("/embed/"):
            video_id = parsed.path.split("/", 2)[2].split("/", 1)[0]

    if not VIDEO_ID_PATTERN.fullmatch(video_id):
        raise LiveYouTubeError("유효한 YouTube 영상 URL이 아닙니다.")
    return video_id


def get_video_metadata(url: str, *, refresh: bool = True) -> dict:
    """Read and persist phase-one public metadata through yt-dlp."""

    thumbnail_files: list[dict] = []
    video_id = extract_video_id(url)
    output_dir = get_media_root() / "yt-data" / video_id
    output_dir.mkdir(parents=True, exist_ok=True)
    info_path = output_dir / f"{video_id}.info.json"
    policy_path = output_dir / f"{video_id}.metadata-policy.json"
    info: dict | None = None
    cache_uses_current_language = False
    try:
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        cache_uses_current_language = (
            isinstance(policy, dict)
            and policy.get("preferred_language") == METADATA_PREFERRED_LANGUAGE
            and policy.get("version") == METADATA_POLICY_VERSION
        )
    except (OSError, json.JSONDecodeError):
        pass
    if not refresh and info_path.exists() and cache_uses_current_language:
        try:
            cached_info = json.loads(info_path.read_text(encoding="utf-8"))
            if isinstance(cached_info, dict):
                info = cached_info
                thumbnail_files = _cached_thumbnail_files(
                    output_dir, video_id, info.get("thumbnail")
                )
                # Older caches predate the thumbnails directory. Reuse their
                # info JSON and only backfill missing image assets; metadata
                # extraction itself is not repeated.
                if not thumbnail_files and info.get("thumbnails"):
                    try:
                        with YoutubeDL(
                            {"quiet": True, "no_warnings": True}
                        ) as downloader:
                            thumbnail_files = _download_thumbnail_list(
                                downloader,
                                info["thumbnails"],
                                info.get("thumbnail"),
                                output_dir,
                                str(info.get("id") or video_id),
                            )
                    except Exception:
                        pass
        except (OSError, json.JSONDecodeError):
            pass

    if info is None:
        try:
            with YoutubeDL(
                {
                    "skip_download": True,
                    "writeinfojson": True,
                    # 1단계 info.json에는 선택 자료의 본문을 수집하지 않는다.
                    # 자막·캡션·채팅의 트랙 목록은 일반 메타데이터로 유지된다.
                    "writecomments": False,
                    "writesubtitles": False,
                    "writeautomaticsub": False,
                    "extractor_args": {
                        "youtube": {
                            "lang": [METADATA_PREFERRED_LANGUAGE],
                            "max_comments": ["0"],
                        },
                    },
                    "outtmpl": str(output_dir / "%(id)s.%(ext)s"),
                    "http_headers": {"Accept-Language": "ko-KR,ko;q=0.9"},
                    "quiet": True,
                    "no_warnings": True,
                }
            ) as downloader:
                # download=True persists the unmodified yt-dlp info JSON.
                info = downloader.extract_info(url, download=True)
                if isinstance(info, dict):
                    try:
                        localized_chapters = _localized_youtube_chapters(
                            downloader, url, info.get("duration")
                        )
                    except Exception:
                        localized_chapters = []
                    if localized_chapters:
                        info["chapters"] = localized_chapters
                    thumbnail_files = _download_thumbnail_list(
                        downloader,
                        info.get("thumbnails") or [],
                        info.get("thumbnail"),
                        output_dir,
                        str(info.get("id") or video_id),
                    )
        except Exception as exc:
            raise LiveYouTubeError(
                f"YouTube 메타데이터를 가져오지 못했습니다: {exc}"
            ) from exc

    if not isinstance(info, dict):
        raise LiveYouTubeError("YouTube 메타데이터 형식이 올바르지 않습니다.")
    # writecomments=False는 별도 댓글 파일 생성을 막지만 yt-dlp 버전에
    # 따라 추출 결과 객체와 info.json에 comments 본문이 남을 수 있다.
    # 이미 받은 댓글을 버리면 선택 수집 단계가 같은 댓글을 다시 요청하므로,
    # comments.json 캐시로 먼저 승격한 뒤 메타데이터 본문에서 제거한다.
    embedded_comments = info.pop("comments", None)
    comments_path = output_dir / f"{video_id}.comments.json"
    if isinstance(embedded_comments, list) and not comments_path.is_file():
        comments_path.write_text(
            json.dumps(embedded_comments, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    try:
        info = format_info_json(
            info_path,
            info,
            prefer_fallback=True,
        )
        format_info_json(
            policy_path,
            {
                "preferred_language": METADATA_PREFERRED_LANGUAGE,
                "version": METADATA_POLICY_VERSION,
            },
            prefer_fallback=True,
        )
    except YouTubeImportError as exc:
        raise LiveYouTubeError(str(exc)) from exc
    duration = info.get("duration")
    if (
        not isinstance(duration, (int, float))
        or isinstance(duration, bool)
        or not 600 <= duration < 21_600
    ):
        raise LiveYouTubeError("10분 이상 6시간 미만 영상만 지원합니다.")

    subtitles = info.get("subtitles") or {}
    automatic_captions = info.get("automatic_captions") or {}
    # yt-dlp places a `live_chat` pseudo-subtitle in `subtitles`. It is a
    # chat stream, not a user-uploaded subtitle track.
    uploaded_subtitles = {
        language: tracks
        for language, tracks in subtitles.items()
        if language != "live_chat"
    }
    live_chat_tracks = subtitles.get("live_chat") or []
    live_status = str(info.get("live_status") or "")
    chat_replay = info.get("chat_replay")
    if not isinstance(chat_replay, bool):
        chat_replay = any(
            isinstance(track, dict)
            and track.get("protocol") == "youtube_live_chat_replay"
            for track in live_chat_tracks
        )
        if not chat_replay:
            # The YouTube extractor does not consistently expose a separate
            # `chat_replay` key. A completed livestream is the yt-dlp signal
            # that a replay chat can be requested; the editing job still
            # verifies it before using the messages.
            chat_replay = live_status in {"was_live", "post_live"}

    def language_options(
        tracks_by_language: dict, *, prefer_korean: bool = False
    ) -> list[dict]:
        values = []
        for language, tracks in tracks_by_language.items():
            if not isinstance(tracks, list):
                continue
            first = next((track for track in tracks if isinstance(track, dict)), {})
            label = (
                first.get("name") if isinstance(first.get("name"), str) else language
            )
            values.append({"value": language, "label": label})
        if prefer_korean:
            korean = [item for item in values if item["value"] == "ko"]
            if korean:
                return korean
        return values

    metadata = {
        "source_url": url,
        "thumbnail": next(
            (item["url"] for item in thumbnail_files if item.get("is_primary")),
            info.get("thumbnail"),
        ),
        "thumbnails": info.get("thumbnails") or [],
        "thumbnail_files": thumbnail_files,
        "title": info.get("title"),
        "video_id": info.get("id"),
        "description": info.get("description"),
        "channel": info.get("channel") or info.get("uploader"),
        "upload_date": info.get("upload_date"),
        "duration_seconds": info.get("duration"),
        "view_count": info.get("view_count"),
        "like_count": info.get("like_count"),
        "comment_count": info.get("comment_count"),
        "categories": info.get("categories") or [],
        "tags": info.get("tags") or [],
        "subtitles_available": bool(uploaded_subtitles),
        "captions_available": bool(automatic_captions),
        "subtitle_languages": language_options(uploaded_subtitles),
        # yt-dlp includes machine-translated caption targets here. In the
        # Korean client, prefer the Korean track when it is available.
        "caption_languages": language_options(automatic_captions, prefer_korean=True),
        "chapters": info.get("chapters") or [],
        "heatmap": info.get("heatmap") or [],
        "chat_replay_available": chat_replay,
        "live_status": live_status or None,
        # Compatibility fields used by the legacy live-finalization response.
        "duration_iso": info.get("duration_string"),
        "live_details": {"live_status": live_status} if live_status else {},
    }
    info_path = output_dir / f"{video_id}.info.json"
    return {**metadata, "metadata_path": str(info_path.resolve())}


def download_metadata_materials(
    url: str,
    selections: dict[str, bool],
    progress_callback: Callable[[int, str], None] | None = None,
) -> dict:
    """Download selected optional metadata assets and return small inspection previews."""

    def report(progress: int, message: str) -> None:
        if progress_callback:
            progress_callback(progress, message)

    # 1단계가 남긴 info JSON은 이 단계의 선택 가능 여부와 언어 목록에
    # 충분하다. 여기서 다시 원격 메타데이터를 조회하면 자료 하나를 받기
    # 전에 yt-dlp 호출이 한 번 더 발생한다.
    report(5, "저장된 영상 메타데이터를 불러오는 중입니다.")
    metadata = get_video_metadata(url, refresh=False)
    video_id = str(metadata["video_id"])
    output_dir = get_media_root() / "yt-data" / video_id
    artifacts: list[dict] = []
    selected_labels = [
        label
        for key, label in (
            ("comments", "댓글"),
            ("chat", "채팅"),
            ("subtitles", "자막"),
            ("captions", "캡션"),
        )
        if selections.get(key)
    ]
    material_weights = {"댓글": 15, "채팅": 15, "자막": 5, "캡션": 5}
    completed_weight = 0
    highest_progress = 0

    def begin_material(label: str) -> None:
        report(
            10 + completed_weight,
            f"{label} 자료를 확인하는 중입니다.",
        )

    def complete_material(label: str) -> None:
        nonlocal completed_weight
        completed_weight += material_weights[label]
        report(
            10 + completed_weight,
            f"{label} 자료를 준비했습니다.",
        )

    def run_ytdlp(options: dict) -> dict:
        try:
            with YoutubeDL(options) as downloader:
                result = downloader.extract_info(url, download=True)
        except Exception as exc:
            raise LiveYouTubeError(
                f"추가 메타데이터 다운로드에 실패했습니다: {exc}"
            ) from exc
        if not isinstance(result, dict):
            raise LiveYouTubeError("yt-dlp가 올바른 메타데이터를 반환하지 않았습니다.")
        return result

    if selections.get("comments"):
        begin_material("댓글")
        if not metadata.get("comment_count"):
            raise LiveYouTubeError("댓글이 없는 영상은 댓글을 다운로드할 수 없습니다.")
        raw_path = output_dir / f"{video_id}.comments.json"
        timestamp_path = (
            _metadata_edit_dir(video_id) / f"{video_id}.comments-timestamps.json"
        )
        comments: list | None = None
        if raw_path.is_file():
            try:
                cached_comments = json.loads(raw_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                cached_comments = None
            if isinstance(cached_comments, list):
                comments = cached_comments
        if comments is None:
            info = run_ytdlp(
                {
                    "skip_download": True,
                    # 댓글은 comments.json에만 보관하고 기존 info.json은 건드리지 않는다.
                    "writeinfojson": False,
                    "writecomments": True,
                    "extractor_args": {"youtube": {"comment_sort": ["top"]}},
                    "outtmpl": str(output_dir / "%(id)s.%(ext)s"),
                    "quiet": True,
                    "no_warnings": True,
                }
            )
            comments = (
                info.get("comments") if isinstance(info.get("comments"), list) else []
            )
            raw_path.write_text(
                json.dumps(comments, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        top_level_comments = [
            comment
            for comment in comments
            if isinstance(comment, dict) and comment.get("parent") in (None, "root")
        ]
        top_level_comments.sort(
            key=lambda comment: (
                _as_sort_number(comment.get("like_count")),
                _as_sort_number(comment.get("timestamp")),
            ),
            reverse=True,
        )
        timestamp_comments = [
            comment
            for comment in top_level_comments
            if re.search(r"(?:\d{1,2}:)?\d{1,2}:\d{2}", str(comment.get("text") or ""))
        ]
        timestamp_path.write_text(
            json.dumps(timestamp_comments, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        artifacts.append(
            {
                "kind": "comments",
                "label": "댓글",
                "path": str(raw_path.resolve()),
                "analysis_path": str(timestamp_path.resolve()),
                "format": "JSON",
                "count": len(top_level_comments),
                "timestamp_count": len(timestamp_comments),
                "total_count": metadata.get("comment_count"),
                "preview": top_level_comments,
            }
        )
        complete_material("댓글")

    if selections.get("chat"):
        begin_material("채팅")
        if not metadata.get("chat_replay_available"):
            raise LiveYouTubeError("채팅 리플레이를 지원하지 않는 영상입니다.")
        paths = _live_chat_jsonl_files(output_dir)
        if not paths:
            run_ytdlp(
                {
                    "skip_download": True,
                    "writecomments": False,
                    "writesubtitles": True,
                    "subtitleslangs": ["live_chat"],
                    "subtitlesformat": "json",
                    "extractor_args": {"youtube": {"max_comments": ["0"]}},
                    "outtmpl": str(output_dir / "%(id)s.%(ext)s"),
                    "quiet": True,
                    "no_warnings": True,
                }
            )
            paths = _live_chat_jsonl_files(output_dir)
        if not paths:
            raise LiveYouTubeError("yt-dlp가 채팅 리플레이 파일을 제공하지 않았습니다.")
        path = paths[0]
        preview = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                preview.extend(_normalize_ytdlp_replay_action(json.loads(line)))
            except json.JSONDecodeError:
                continue
        times_path = _metadata_edit_dir(video_id) / f"{video_id}.chat-times.json"
        chat_times = []
        for item in preview:
            if not isinstance(item.get("elapsed_seconds"), (int, float)):
                continue
            # 분석 파일에는 원본 채널 ID나 표시 이름을 남기지 않는다. 영상별
            # 식별자는 동일 작성자의 반복 채팅만 판별할 수 있으면 충분하다.
            identity = str(
                item.get("author_id") or item.get("author") or item.get("id") or ""
            )
            if not identity:
                continue
            anonymous_id = hashlib.sha256(
                f"{video_id}\0{identity}".encode("utf-8")
            ).hexdigest()[:20]
            chat_times.append(
                {
                    "elapsed_seconds": item["elapsed_seconds"],
                    "author_id": anonymous_id,
                }
            )
        pending_times_path = times_path.with_name(
            f".{times_path.name}.{uuid4().hex}.tmp"
        )
        try:
            pending_times_path.write_text(
                json.dumps(chat_times, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(pending_times_path, times_path)
        finally:
            pending_times_path.unlink(missing_ok=True)
        artifacts.append(
            {
                "kind": "chat",
                "label": "채팅",
                "path": str(path.resolve()),
                "analysis_path": str(times_path.resolve()),
                "format": "JSONL",
                "count": len(preview),
                "preview": preview,
            }
        )
        complete_material("채팅")

    for kind, option, available, label, language_key in (
        (
            "subtitles",
            "writesubtitles",
            metadata.get("subtitles_available"),
            "자막",
            "subtitle_language",
        ),
        (
            "captions",
            "writeautomaticsub",
            metadata.get("captions_available"),
            "캡션",
            "caption_language",
        ),
    ):
        if not selections.get(kind):
            continue
        begin_material(label)
        if not available:
            raise LiveYouTubeError(f"{label}을(를) 지원하지 않는 영상입니다.")
        language = str(selections.get(language_key) or "")
        available_languages = (
            metadata.get(
                "subtitle_languages" if kind == "subtitles" else "caption_languages"
            )
            or []
        )
        available_values = {
            item.get("value") for item in available_languages if isinstance(item, dict)
        }
        if language not in available_values:
            raise LiveYouTubeError(f"다운로드할 {label} 언어를 선택하세요.")
        (output_dir / kind).mkdir(parents=True, exist_ok=True)
        paths = sorted((output_dir / kind).glob(f"{video_id}.{language}*.vtt"))
        if paths:
            paths = [paths[0]]
        else:
            options = {
                "skip_download": True,
                "writecomments": False,
                option: True,
                "subtitleslangs": [language],
                "subtitlesformat": "vtt",
                "extractor_args": {"youtube": {"max_comments": ["0"]}},
                "outtmpl": str(output_dir / kind / "%(id)s.%(ext)s"),
                "quiet": True,
                "no_warnings": True,
            }
            run_ytdlp(options)
            paths = sorted((output_dir / kind).glob(f"{video_id}.{language}*.vtt"))
            if not paths:
                raise LiveYouTubeError(f"yt-dlp가 {label} 파일을 제공하지 않았습니다.")
        previews = [
            row
            for path in paths
            for row in (
                _clean_caption_rows(
                    _rolling_caption_rows(
                        _parse_vtt_rows(
                            path.read_text(encoding="utf-8", errors="replace"),
                            path.name,
                        )
                    )
                )
                if kind == "captions"
                else _parse_vtt_rows(
                    path.read_text(encoding="utf-8", errors="replace"), path.name
                )
            )
        ]
        edit_dir = _metadata_edit_dir(video_id)
        # 업로드 자막은 yt-dlp 원본 VTT를 그대로 표시한다. 캡션만 롤링
        # 중복을 제거한 VTT를 yt-edit에 별도로 만든다.
        display_path = paths[0]
        if kind == "captions":
            display_path = edit_dir / f"{video_id}.{language}.captions-rolling.vtt"
            _write_display_vtt(display_path, previews)
        transcript_segments = [
            {
                "start": round(_vtt_timestamp_seconds(str(row["start"])), 3),
                "end": round(_vtt_timestamp_seconds(str(row["end"])), 3),
                "text": str(row["text"]),
                "words": row.get("words", []),
            }
            for row in previews
            if row.get("text")
            and _vtt_timestamp_seconds(str(row["end"]))
            > _vtt_timestamp_seconds(str(row["start"]))
        ]
        parsed_path = edit_dir / f"{video_id}.{language}.{kind}-transcript.json"
        parsed_path.write_text(
            json.dumps({"segments": transcript_segments}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        artifacts.append(
            {
                "kind": kind,
                "label": label,
                "path": str(display_path.resolve()),
                "parsed_path": str(parsed_path.resolve()),
                "format": "WebVTT",
                "count": len(previews),
                "preview": previews,
            }
        )
        complete_material(label)

    # 분석 단계는 원격 수집을 하지 않는다. 선택한 추가 자료가 없거나
    # Whisper만 사용하는 경우에도 2단계에서 원본 영상을 확보한다.
    video_start = 10 + completed_weight
    report(video_start, "분석용 원본 영상을 준비하는 중입니다.")

    def video_progress(percent: int) -> None:
        nonlocal highest_progress
        value = video_start + round(max(0, min(100, percent)) * (98 - video_start) / 100)
        if value > highest_progress:
            highest_progress = value
            report(
                value,
                "원본 영상 준비를 완료했습니다."
                if percent >= 100 else f"원본 영상 다운로드 중 ({percent}%)",
            )

    try:
        YouTubeImporter(get_media_root()).prepare_source_video(
            url,
            job_id=video_id,
            include_subtitles=False,
            progress_callback=video_progress,
        )
    except (YouTubeImportError, OSError) as exc:
        raise LiveYouTubeError(
            f"분석용 원본 영상을 준비하지 못했습니다: {exc}"
        ) from exc

    report(100, "추가 메타데이터 준비를 완료했습니다.")
    return {"video_id": video_id, "artifacts": artifacts}

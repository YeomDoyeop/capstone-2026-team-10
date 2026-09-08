import json
from io import BytesIO

import pytest

from app.services.live_youtube_service import (
    LiveYouTubeError,
    _download_thumbnail_list,
    _parse_vtt_rows,
    _rolling_caption_rows,
    download_metadata_materials,
    get_video_metadata,
)


def test_rolling_caption_completion_markers_restore_spoken_intervals():
    rows = _parse_vtt_rows(
        """WEBVTT

00:00:00.040 --> 00:00:01.949
첫 구간

00:00:01.949 --> 00:00:01.959
첫 구간

00:00:01.959 --> 00:00:02.000


00:00:02.000 --> 00:00:04.030

첫 구간 둘째 구간

00:00:04.030 --> 00:00:04.040
둘째 구간
""",
        "sample.ko.vtt",
    )

    completed = _rolling_caption_rows(rows)

    assert [(row["start"], row["end"], row["text"]) for row in completed] == [
        ("00:00:00.040", "00:00:01.949", "첫 구간"),
        ("00:00:02.000", "00:00:04.030", "둘째 구간"),
    ]


def test_thumbnail_list_saves_every_ytdlp_thumbnail_entry(tmp_path):
    class Response:
        def read(self):
            return b"thumbnail bytes"

        def close(self):
            pass

    class Downloader:
        urls = []

        def urlopen(self, url):
            self.urls.append(url)
            return Response()

    saved = _download_thumbnail_list(
        Downloader(),
        [
            {"url": "https://i.ytimg.com/vi/video/sddefault.jpg"},
            {"url": "https://i.ytimg.com/vi/video/sd1.jpg"},
            {"url": "https://i.ytimg.com/vi/video/sd2.jpg"},
            {"url": "https://i.ytimg.com/vi/video/sd3.jpg"},
        ],
        None,
        tmp_path,
        "video-id",
    )

    assert len(saved) == 4
    assert saved[0]["is_primary"] is True
    assert (tmp_path / "thumbnails" / "sd3.jpg").read_bytes() == b"thumbnail bytes"


def test_metadata_uses_existing_info_json_without_calling_ytdlp(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_ROOT", str(tmp_path))
    video_id = "abc123def45"
    output_dir = tmp_path / "yt-data" / video_id
    output_dir.mkdir(parents=True)
    (output_dir / f"{video_id}.info.json").write_text(
            json.dumps({"id": video_id, "title": "cached title", "thumbnail": "https://example.com/default.jpg", "duration": 600}),
        encoding="utf-8",
    )
    (output_dir / f"{video_id}.metadata-policy.json").write_text(
        json.dumps({"preferred_language": "ko", "version": 4}), encoding="utf-8"
    )

    result = get_video_metadata(f"https://www.youtube.com/watch?v={video_id}", refresh=False)

    assert result["title"] == "cached title"
    assert result["video_id"] == video_id


def test_metadata_info_download_excludes_optional_material_bodies(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_ROOT", str(tmp_path))
    video_id = "abc123def45"
    calls = []

    class Downloader:
        def __init__(self, options):
            calls.append(options)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, _url, download=True):
            return {"id": video_id, "title": "title", "duration": 600}

    monkeypatch.setattr("app.services.live_youtube_service.YoutubeDL", Downloader)

    get_video_metadata(f"https://www.youtube.com/watch?v={video_id}")

    options = calls[0]
    assert options["writeinfojson"] is True
    assert options["writecomments"] is False
    assert options["writesubtitles"] is False
    assert options["writeautomaticsub"] is False
    assert options["extractor_args"]["youtube"]["lang"] == ["ko"]
    assert options["extractor_args"]["youtube"]["max_comments"] == ["0"]


def test_phase_one_info_json_removes_comment_bodies_but_keeps_count(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_ROOT", str(tmp_path))
    video_id = "abc123def45"

    class Downloader:
        def __init__(self, _options):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, _url, download=True):
            return {
                "id": video_id,
                "title": "title",
                "duration": 600,
                "comment_count": 2,
                "comments": [{"id": "one"}, {"id": "two"}],
                "description": "타임라인\n00:00 한국어 시작\n01:30 한국어 본문",
                "chapters": [
                    {"start_time": 0, "end_time": 90, "title": "English opening"},
                    {"start_time": 90, "end_time": 600, "title": "English body"},
                ],
            }

        def urlopen(self, url):
            assert "hl=ko" in url
            assert "gl=KR" in url
            return BytesIO(
                b'<script>var ytInitialData = {"playerOverlays":{"playerOverlayRenderer":'
                b'{"decoratedPlayerBarRenderer":{"playerBar":{"multiMarkersPlayerBarRenderer":'
                b'{"markersMap":[{"key":"DESCRIPTION_CHAPTERS","value":{"chapters":['
                b'{"chapterRenderer":{"title":{"simpleText":"\\uc624\\ud504\\ub2dd"},"timeRangeStartMillis":0}},'
                b'{"chapterRenderer":{"title":{"simpleText":"AI \\ubcf8\\ubb38"},"timeRangeStartMillis":90000}}'
                b']}}]}}}}}};</script>'
            )

    monkeypatch.setattr("app.services.live_youtube_service.YoutubeDL", Downloader)

    get_video_metadata(f"https://www.youtube.com/watch?v={video_id}")

    saved = json.loads(
        (tmp_path / "yt-data" / video_id / f"{video_id}.info.json").read_text(encoding="utf-8")
    )
    assert saved["comment_count"] == 2
    assert "comments" not in saved
    assert saved["chapters"] == [
        {"start_time": 0.0, "end_time": 90.0, "title": "오프닝"},
        {"start_time": 90.0, "end_time": 600.0, "title": "AI 본문"},
    ]


def test_comment_download_does_not_overwrite_info_json(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_ROOT", str(tmp_path))
    video_id = "abc123def45"
    output_dir = tmp_path / "yt-data" / video_id
    output_dir.mkdir(parents=True)
    info_path = output_dir / f"{video_id}.info.json"
    original_info = {"id": video_id, "title": "title", "duration": 600, "comment_count": 1}
    info_path.write_text(json.dumps(original_info), encoding="utf-8")
    (output_dir / f"{video_id}.metadata-policy.json").write_text(
        json.dumps({"preferred_language": "ko", "version": 4}), encoding="utf-8"
    )
    calls = []

    class Downloader:
        def __init__(self, options):
            calls.append(options)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, _url, download=True):
            return {"comments": [{"id": "comment", "parent": "root", "text": "0:10"}]}

    monkeypatch.setattr("app.services.live_youtube_service.YoutubeDL", Downloader)
    monkeypatch.setattr(
        "app.services.live_youtube_service.YouTubeImporter.prepare_source_video",
        lambda *_args, **_kwargs: {},
    )

    download_metadata_materials(
        f"https://www.youtube.com/watch?v={video_id}",
        {"comments": True, "chat": False, "subtitles": False, "captions": False},
    )

    comment_options = calls[0]
    assert comment_options["writecomments"] is True
    assert comment_options["writeinfojson"] is False
    assert json.loads(info_path.read_text(encoding="utf-8")) == original_info


def test_material_step_prepares_video_without_subtitles_when_nothing_is_selected(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_ROOT", str(tmp_path))
    video_id = "abc123def45"
    output_dir = tmp_path / "yt-data" / video_id
    output_dir.mkdir(parents=True)
    (output_dir / f"{video_id}.info.json").write_text(
        json.dumps({"id": video_id, "title": "title", "duration": 600}),
        encoding="utf-8",
    )
    (output_dir / f"{video_id}.metadata-policy.json").write_text(
        json.dumps({"preferred_language": "ko", "version": 4}),
        encoding="utf-8",
    )
    calls = []

    def prepare_source(_self, url, job_id=None, *, include_subtitles=True):
        calls.append((url, job_id, include_subtitles))
        return {}

    monkeypatch.setattr(
        "app.services.live_youtube_service.YouTubeImporter.prepare_source_video",
        prepare_source,
    )

    result = download_metadata_materials(
        f"https://www.youtube.com/watch?v={video_id}",
        {"comments": False, "chat": False, "subtitles": False, "captions": False},
    )

    assert result["artifacts"] == []
    assert calls == [
        (f"https://www.youtube.com/watch?v={video_id}", video_id, False),
    ]


def test_legacy_metadata_cache_is_refreshed_with_korean_preference(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_ROOT", str(tmp_path))
    video_id = "abc123def45"
    output_dir = tmp_path / "yt-data" / video_id
    output_dir.mkdir(parents=True)
    (output_dir / f"{video_id}.info.json").write_text(
        json.dumps({"id": video_id, "title": "English", "duration": 600}),
        encoding="utf-8",
    )
    (output_dir / f"{video_id}.metadata-policy.json").write_text(
        json.dumps({"preferred_language": "ko"}), encoding="utf-8"
    )
    calls = []

    class Downloader:
        def __init__(self, options):
            calls.append(options)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, _url, download=True):
            return {"id": video_id, "title": "한국어", "duration": 600}

    monkeypatch.setattr("app.services.live_youtube_service.YoutubeDL", Downloader)

    result = get_video_metadata(
        f"https://www.youtube.com/watch?v={video_id}", refresh=False
    )

    assert result["title"] == "한국어"
    assert calls[0]["extractor_args"]["youtube"]["lang"] == ["ko"]
    policy = json.loads(
        (output_dir / f"{video_id}.metadata-policy.json").read_text(encoding="utf-8")
    )
    assert policy == {"preferred_language": "ko", "version": 4}


@pytest.mark.parametrize("duration", [599, 21_600])
def test_metadata_rejects_videos_outside_supported_duration(tmp_path, monkeypatch, duration):
    monkeypatch.setenv("MEDIA_ROOT", str(tmp_path))
    video_id = "abc123def45"
    output_dir = tmp_path / "yt-data" / video_id
    output_dir.mkdir(parents=True)
    (output_dir / f"{video_id}.info.json").write_text(
        json.dumps({"id": video_id, "title": "cached title", "duration": duration}), encoding="utf-8"
    )
    (output_dir / f"{video_id}.metadata-policy.json").write_text(
        json.dumps({"preferred_language": "ko", "version": 4}), encoding="utf-8"
    )

    with pytest.raises(LiveYouTubeError, match="10분 이상 6시간 미만"):
        get_video_metadata(f"https://www.youtube.com/watch?v={video_id}", refresh=False)

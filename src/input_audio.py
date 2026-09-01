import ipaddress
import os
import socket
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import requests


class AudioDownloadError(ValueError):
    code = "AUDIO_DOWNLOAD_FAILED"


def download_audio(audio_url: str, directory: Path) -> Path:
    current_url = audio_url
    timeout = int(os.environ.get("DOWNLOAD_TIMEOUT_SECONDS", "60"))
    maximum_bytes = int(os.environ.get("MAX_DOWNLOAD_BYTES", str(512 * 1024 * 1024)))

    for _ in range(4):
        _validate_public_https_url(current_url)
        try:
            response = requests.get(current_url, stream=True, timeout=timeout, allow_redirects=False)
        except requests.RequestException as error:
            raise AudioDownloadError("오디오 URL을 다운로드할 수 없습니다.") from error
        if response.is_redirect:
            location = response.headers.get("Location")
            if not location:
                raise AudioDownloadError("오디오 URL의 리디렉션 위치가 없습니다.")
            current_url = requests.compat.urljoin(current_url, location)
            continue
        if response.status_code != 200:
            raise AudioDownloadError("오디오 URL을 다운로드할 수 없습니다.")

        content_length = response.headers.get("Content-Length")
        try:
            declared_size = int(content_length) if content_length else None
        except ValueError as error:
            raise AudioDownloadError("오디오 파일 크기 정보가 올바르지 않습니다.") from error
        if declared_size and declared_size > maximum_bytes:
            raise AudioDownloadError("오디오 파일이 최대 크기를 초과했습니다.")

        target = directory / "input.audio"
        downloaded = 0
        with target.open("wb") as file:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                downloaded += len(chunk)
                if downloaded > maximum_bytes:
                    raise AudioDownloadError("오디오 파일이 최대 크기를 초과했습니다.")
                file.write(chunk)
        return target

    raise AudioDownloadError("리디렉션이 너무 많습니다.")


def apply_speed(audio_path: Path, speed: float) -> Path:
    output_path = audio_path.with_name("speed-adjusted.wav")
    filters = _atempo_filters(speed)
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(audio_path), "-filter:a", filters, str(output_path)],
            check=True,
            capture_output=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise AudioDownloadError("배속 오디오를 만들 수 없습니다.") from error
    return output_path


def _atempo_filters(speed: float) -> str:
    filters: list[str] = []
    while speed > 2.0:
        filters.append("atempo=2.0")
        speed /= 2.0
    filters.append(f"atempo={speed}")
    return ",".join(filters)


def _validate_public_https_url(value: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname:
        raise AudioDownloadError("audio_url은 공개 https URL이어야 합니다.")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, None)}
    except socket.gaierror as error:
        raise AudioDownloadError("audio_url의 호스트를 찾을 수 없습니다.") from error

    if any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise AudioDownloadError("audio_url은 공개 인터넷 주소여야 합니다.")

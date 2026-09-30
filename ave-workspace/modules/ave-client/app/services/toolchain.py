"""Windows 배포물에 포함한 영상 도구의 경로와 실행을 관리한다."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


class ToolchainError(RuntimeError):
    pass


def _client_root() -> Path:
    return Path(__file__).resolve().parents[2]


def bin_directory() -> Path:
    """클라이언트 루트의 `/bin`을 찾는다."""

    configured = os.getenv("AVE_BIN_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return _client_root() / "bin"


def executable(name: str) -> Path:
    # 이 모듈은 Windows 배포 바이너리만 지원한다. 개발 환경이 WSL이어도
    # 동일한 계약으로 Windows `.exe` 파일을 검증한다.
    filename = f"{name}.exe"
    path = bin_directory() / filename
    if not path.is_file():
        raise ToolchainError(
            f"필수 바이너리 `{filename}`을(를) 찾지 못했습니다. "
            f"`{bin_directory()}`에 배치하세요."
        )
    return path


def ffmpeg() -> Path:
    return executable("ffmpeg")


def ffprobe() -> Path:
    return executable("ffprobe")


def ytdlp() -> Path:
    return executable("yt-dlp")


def version(name: str) -> str:
    version_argument = "--version" if name == "yt-dlp" else "-version"
    try:
        completed = subprocess.run(
            [str(executable(name)), version_argument],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=15,
        )
    except OSError as exc:
        raise ToolchainError(f"`{name}`을(를) 실행할 수 없습니다: {exc}") from exc
    if completed.returncode != 0:
        raise ToolchainError(
            completed.stderr.strip() or f"`{name}` 버전을 확인하지 못했습니다."
        )
    return completed.stdout.strip().splitlines()[0]


def diagnostics() -> dict[str, str]:
    """UI·트레이 로그에서 표시할 수 있는 최소 실행 환경 진단 결과다."""

    result: dict[str, str] = {}
    for name in ("yt-dlp", "ffmpeg", "ffprobe"):
        try:
            result[name] = version(name)
        except ToolchainError as exc:
            result[name] = f"오류: {exc}"
    return result

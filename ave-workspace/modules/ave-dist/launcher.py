"""PyInstaller 포터블 앱의 실행 환경을 설정하고 클라이언트를 시작한다."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def _application_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _bundled_root() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def _hide_child_process_consoles() -> None:
    """windowed 배포 앱이 실행하는 CLI 도구의 콘솔 창을 숨긴다."""

    if os.name != "nt":
        return

    original_popen = subprocess.Popen

    class NoWindowPopen(original_popen):
        def __init__(self, *args, **kwargs):
            creation_flags = int(kwargs.get("creationflags", 0))
            kwargs["creationflags"] = creation_flags | subprocess.CREATE_NO_WINDOW
            super().__init__(*args, **kwargs)

    subprocess.Popen = NoWindowPopen


def main() -> None:
    application_root = _application_root()
    os.chdir(application_root)
    os.environ.setdefault("MEDIA_ROOT", str(application_root / "media"))
    os.environ.setdefault("DB_ROOT", str(application_root / "db"))
    os.environ.setdefault("AVE_BIN_DIR", str(application_root / "bin"))
    _hide_child_process_consoles()

    # 클라이언트 모듈이 import되기 전에 포터블 앱 루트의 설정을 읽는다.
    from dotenv import load_dotenv

    load_dotenv(_bundled_root() / ".env", override=True)

    from fastapi.staticfiles import StaticFiles
    from app.services import prompt_store

    static_root = application_root / "static"
    ui_root = static_root / "ui"
    if not (ui_root / "index.html").is_file():
        raise RuntimeError(f"정적 UI를 찾을 수 없습니다: {ui_root}")
    prompt_store.PROMPT_ROOT = application_root / "prompts"
    prompt_store.USER_PROMPT_DIR = prompt_store.PROMPT_ROOT / "user"
    prompt_store.SYSTEM_PROMPT_DIR = prompt_store.PROMPT_ROOT / "system"
    prompt_store.SCHEMA_DIR = prompt_store.PROMPT_ROOT / "schemas"

    # 앱 모듈은 import 도중에도 응답 스키마를 읽으므로 프롬프트 경로를 먼저 설정한다.
    from app import main as client_main

    client_main.STATIC_DIR = static_root
    client_main.REACT_UI_DIR = ui_root
    if (ui_root / "assets").is_dir():
        client_main.app.mount(
            "/ui/assets",
            StaticFiles(directory=ui_root / "assets"),
            name="react-assets",
        )

    from app import desktop

    # frozen 모듈의 내부 위치가 아니라 사용자에게 보이는 앱 루트에 기록한다.
    desktop.LOG_PATH = application_root / "client.log"
    desktop.main()


if __name__ == "__main__":
    main()

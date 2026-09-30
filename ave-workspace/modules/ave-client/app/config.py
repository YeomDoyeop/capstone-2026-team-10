import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


def get_media_root() -> Path:
    return Path(os.getenv("MEDIA_ROOT", "media")).resolve()


def get_database_root() -> Path:
    return Path(os.getenv("DB_ROOT", "db")).resolve()


def get_ave_server_url() -> str:
    return os.getenv("AVE_SERVER_URL", "").strip().rstrip("/")


def get_whisper_heartbeat_seconds() -> int:
    return max(1, int(os.getenv("WHISPER_HEARTBEAT_SECONDS", "10")))

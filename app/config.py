"""서버 환경 설정을 읽는다."""

import os
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


def get_supabase_url() -> str:
    return os.getenv("SUPABASE_URL", "").strip()


def get_supabase_anon_key() -> str:
    return os.getenv("SUPABASE_ANON_KEY", "").strip()


def get_supabase_service_role_key() -> str:
    return os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()


def get_public_base_url() -> str:
    return os.getenv("AVE_PUBLIC_BASE_URL", "").strip().rstrip("/")


def get_stt_files_dir() -> Path:
    return Path(os.getenv("STT_FILES_DIR", "/srv/ave-stt/files")).resolve()


def get_stt_file_max_bytes() -> int:
    return int(os.getenv("STT_FILE_MAX_BYTES", str(1024 * 1024 * 1024)))


def get_whisper_runpod_endpoint_id() -> str:
    return os.getenv("WHISPER_RUNPOD_ENDPOINT_ID", "").strip()


def get_runpod_api_key() -> str:
    return os.getenv("RUNPOD_API_KEY", "").strip()


def get_whisper_runpod_timeout_seconds() -> int:
    return int(os.getenv("WHISPER_RUNPOD_TIMEOUT_SECONDS", "3600"))


def get_transcription_lease_seconds() -> int:
    return int(os.getenv("TRANSCRIPTION_LEASE_SECONDS", "60"))


def get_transcription_lease_sweep_seconds() -> int:
    return int(os.getenv("TRANSCRIPTION_LEASE_SWEEP_SECONDS", "15"))

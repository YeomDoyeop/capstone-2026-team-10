#!/usr/bin/env bash
set -euo pipefail

readonly HOURS="${1:-24}"
readonly DIRECTORY="/srv/ave-stt/files"

[[ "$HOURS" =~ ^[0-9]+$ ]] || { echo "시간은 0 이상의 정수여야 합니다." >&2; exit 1; }
find "$DIRECTORY" -xdev -type f -mmin "+$((HOURS * 60))" -delete

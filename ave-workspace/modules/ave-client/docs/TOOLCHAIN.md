# 로컬 영상 도구 체인

클라이언트는 Windows x64 전용이며 영상 처리 도구를 Python 라이브러리나 시스템 `PATH`에서 찾지 않는다. 배포물의 루트 `/bin`에 있는 독립 실행 파일만 사용한다.

```text
bin/
├─ yt-dlp.exe
├─ ffmpeg.exe
└─ ffprobe.exe
```

`bin/`은 yt-dlp와 FFmpeg의 라이선스 검토가 끝날 때까지 Git 저장소와 배포물에 포함하지 않는다. 개발자는 아래 준비 절차로 각자의 로컬 환경에만 도구를 내려받는다.

## 도구 설치

클라이언트 모듈 루트에서 다음 명령을 실행해 Windows x64용 `yt-dlp.exe`, `ffmpeg.exe`, `ffprobe.exe`를 로컬 `bin/`에 내려받는다.

```powershell
powershell.exe -ExecutionPolicy Bypass -File .\scripts\fetch-tools.ps1
```

`Bypass`는 이 명령으로 시작한 PowerShell 프로세스에만 적용되며, 사용자 또는 시스템의 실행 정책을 변경하지 않는다.

## 역할

| 도구 | 역할 |
| --- | --- |
| `yt-dlp.exe` | YouTube 메타데이터, 영상, 자막, 채팅 리플레이 수집 |
| `ffmpeg.exe` | 구간 자르기·결합, 음원 추출, SRT/ASS 자막 합성, 최종 인코딩 |
| `ffprobe.exe` | 향후 미디어 사전 검사 및 진단용 |

MoviePy와 Python `yt-dlp` 패키지는 런타임 의존성으로 사용하지 않는다. `app.services.ytdlp_binary`는 기존 수집 코드가 필요한 옵션을 yt-dlp 명령행 인수로 변환하고, `app.services.toolchain`은 실행 파일의 위치와 버전을 확인한다.

## 개발 준비

1. Windows x64용 `yt-dlp.exe`, `ffmpeg.exe`, `ffprobe.exe`를 `/bin`에 둔다.
2. FFmpeg 배포본은 자막 합성에 필요한 `subtitles` 필터와 libass를 포함해야 한다.
3. 개발 중 다른 디렉터리를 사용해야 하면 `AVE_BIN_DIR`에 그 경로를 설정한다.
실제 공개 배포 전에는 선택한 바이너리의 버전, 해시, 라이선스 고지 파일을 `/bin`에 확정한다.

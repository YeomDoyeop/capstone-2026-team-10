# AVE 클라이언트 배포

이 저장소는 `ave-client`를 Windows x64 포터블 앱으로 패키징하는 코드만 관리합니다. 빌드는 클라이언트 저장소를 읽기만 하며 UI 컴파일, Python 패키징, 임시 파일 생성은 모두 `ave-dist` 안에서 수행합니다.

## 산출물

`build.ps1`은 실행 파일 두 개와 외부 정적 UI를 생성합니다.

```text
release/
├─ AVE-client-<version>-windows-x64/
│  ├─ ave_client.exe
│  ├─ ave_updater.exe
│  ├─ readme.txt
│  ├─ prompts/
│  │  ├─ user/
│  │  ├─ system/
│  │  └─ schemas/
│  └─ static/
│     └─ ui/
└─ AVE-client-<version>-windows-x64.zip
```

`ave_client.exe`는 Python 런타임과 빌드 시점의 `.env`를 포함한 단일 파일입니다. 정적 UI는 실행 파일에 포함하지 않고 루트의 `static/ui`에서 읽습니다. 외부 `prompts/user`에는 UI에서 관리하는 판별 기준을, `prompts/system`에는 호출별 시스템 프롬프트를, `prompts/schemas`에는 JSON 응답 계약을 둡니다. 외부 `.env`나 예시 설정 파일은 만들지 않습니다.

`ave_updater.exe`는 첫 화면에서 `FFmpeg 및 FFprobe`와 `yt-dlp`를 체크박스로 선택하고 **다음**을 눌러야 설치를 시작합니다. 진행 화면에는 현재 작업과 현재·전체 진행률을 표시하며, **중지**를 누르면 임시 파일 정리를 마친 뒤 창을 닫을 수 있습니다. 별도 업데이트 매니페스트를 사용하지 않고 실행 시 최신 바이너리를 직접 받습니다. yt-dlp 공식 GitHub 최신 릴리스에서 `yt-dlp.exe`를 받고, FFmpeg 공식 다운로드 페이지가 Windows 빌드로 안내하는 gyan.dev의 최신 release essentials ZIP에서 `ffmpeg.exe`와 `ffprobe.exe`를 추출합니다. 제공처의 SHA-256도 함께 받아 검증한 뒤 루트의 `bin`에 교체합니다. `ave_client.exe`는 변경하지 않습니다. 업데이터 UI는 운영체제의 기본 글꼴을 사용합니다.

`ave_client.exe`는 클라이언트가 트레이에서 사용하는 검은 배경·주황색 재생 기호 아이콘을 사용하고, `ave_updater.exe`는 같은 색상의 아래 방향 화살표 하나가 있는 다운로드 아이콘을 사용합니다. 작업 DB, 미디어와 로그는 실행 파일이 있는 디렉터리에 생성됩니다.

## 빌드 요구 환경

* Windows x64
* Python 3.11 이상 x64 (`py` 실행기 권장)
* Node.js와 npm
* 형제 디렉터리 `../ave-client`
* `ave-client/bin`의 `yt-dlp.exe`, `ffmpeg.exe`, `ffprobe.exe`

PowerShell에서 실행합니다.

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\build.ps1 `
  -Version 0.1.0
```

클라이언트 경로나 Python 실행기를 명시할 수도 있습니다.

```powershell
.\build.ps1 `
  -Version 0.1.0 `
  -ClientDir C:\work\ave-client `
  -PythonCommand C:\Python311\python.exe `
  -EnvFile C:\secure\ave-client.env
```

빌드는 다음 순서로 동작합니다.

1. 클라이언트 소스와 필수 바이너리를 읽기 전용으로 검증합니다.
2. UI 소스를 `.build/ui`로 복사하고 `npm ci`, `npm run build`를 실행합니다.
3. `.build/venv`에 고정된 PyInstaller와 클라이언트 의존성을 설치합니다.
4. `.env`를 포함한 단일 파일 `ave_client.exe`를 생성하고 정적 UI를 `static/ui`에 복사합니다.
5. 바이너리만 설치·갱신하는 단일 파일 `ave_updater.exe`를 생성합니다.
6. 사용자 안내용 `readme.txt`를 포함하여 배포용 ZIP을 생성합니다.

`-SkipUiBuild`는 현재 `ave-client/static/ui`를 그대로 패키징할 때만 사용합니다. 재현 가능한 공개 빌드에는 기본 UI 빌드를 권장합니다.

## 배포 전 확인

패키징 성공은 앱 기능 전체의 동작을 보장하지 않습니다. 깨끗한 Windows x64 PC에서 다음을 확인해야 합니다.

* 빌드에 사용한 `.env`의 실제 AVE Server URL 설정
* 빈 디렉터리에서 `ave_updater.exe` 실행 후 `bin` 생성 확인
* 트레이 아이콘, 웹 UI, 로그 창, 종료 동작
* YouTube 메타데이터 및 영상 다운로드
* FFmpeg/FFprobe 실행, 자막 렌더링, GPU 실패 시 CPU 전환
* 로그인, 원격 Whisper, LLM 분석, 완료 이력 동기화
* 한글과 공백이 포함된 설치 경로 및 쓰기 권한

`ffmpeg.exe`, `ffprobe.exe`, `yt-dlp.exe`를 외부에 배포하기 전에는 각 바이너리의 정확한 출처, 버전, 빌드 옵션과 라이선스를 별도로 검토해야 합니다. 빌드는 사용된 파일의 SHA-256을 기록하지만 라이선스 적합성을 자동 판정하지 않습니다.

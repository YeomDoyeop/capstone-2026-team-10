# AVE 워크스페이스

AVE(Automatic Video Edit)는 YouTube 영상의 스크립트, 채팅, 댓글 타임스탬프, 히트맵, 음량 등의 정보를 분석해 편집 구간 선정을 돕는 시스템입니다.

이 저장소는 여러 독립 Git 저장소를 한곳에서 개발하기 위한 루트 워크스페이스입니다. 실제 애플리케이션 코드와 실행·배포 설정은 `modules/` 아래 각 모듈에서 관리합니다.

## 구성

| 모듈 | 역할 | 현재 실행 기준 |
| --- | --- | --- |
| `ave-client` | Windows 로컬 서버·트레이·웹 UI, 영상 수집·분석·자막·렌더링 | `python -m app` |
| `ave-server` | 인증, 작업·분석 데이터, LLM 및 원격 STT 중계 API | 모듈의 배포 문서 참고 |
| `ave-whisper-api` | GPU 환경에서 실행하는 faster-whisper 기반 음성 전사 API | RunPod Queue worker 배포 |
| `ave-dist` | 향후 Windows 설치 프로그램과 배포 산출물 관리 | 아직 구현하지 않음 |

원본 영상, 렌더링 결과, 로컬 작업 파일과 경로는 `ave-client`가 실행되는 사용자 PC에만 보관합니다. 서버에는 작업 식별자, 스크립트와 분석 결과, 추천·선택 이력 등 필요한 데이터만 저장합니다. 원격 STT용 오디오는 전사 중에만 임시로 사용한 뒤 삭제합니다.

## 시작하기

### 1. 워크스페이스 준비

Windows에서 Git을 설치한 뒤 루트의 `setup.bat`를 실행합니다. 스크립트는 다음 모듈을 `modules/` 아래에 내려받으며, 이미 존재하는 디렉터리는 그대로 둡니다.

```text
ave-client
ave-dist
ave-server
ave-whisper-api
```

기존에 모듈 저장소를 내려받은 상태라면 이 단계는 건너뛰어도 됩니다.

### 2. 클라이언트 개발 실행

현재 AVE의 로컬 실행 진입점은 `ave-client`입니다. Windows x64와 Python 3.11 이상이 필요합니다.

```powershell
cd modules\ave-client
Copy-Item .env.example .env
```

`.env`에서 AVE Server 주소를 설정합니다.

```env
AVE_SERVER_URL=https://ave-server.example.com
```

영상 도구가 없다면 내려받고, Python 의존성을 설치한 뒤 실행합니다.

```powershell
powershell.exe -ExecutionPolicy Bypass -File .\scripts\fetch-tools.ps1
python -m pip install -r requirements.txt
python -m app
```

실행하면 로컬 FastAPI 서버, 시스템 트레이 아이콘, 기본 웹 UI가 시작됩니다. 필요한 실행 파일은 `bin/yt-dlp.exe`, `bin/ffmpeg.exe`, `bin/ffprobe.exe`입니다.

UI 코드를 수정한 경우에는 별도로 빌드합니다.

```powershell
cd ui
npm install
npm run build
```

### 3. 서버와 원격 STT

`ave-server`는 클라이언트의 로그인·분석 요청을 처리하고 LLM 및 원격 STT를 중계합니다. Ubuntu VM 배포 절차는 [서버 배포 안내](modules/ave-server/deploy/README.md)를 참고합니다.

`ave-whisper-api`는 `faster-whisper`와 GPU를 사용하는 전사 서비스이며, 현재 RunPod Serverless Queue worker 방식으로 배포합니다. 요청 형식과 배포 방법은 [전사 API 안내](modules/ave-whisper-api/README.md)를 참고합니다.

## 개발 시 참고 문서

* [프로젝트 개요와 모듈 경계](docs/PROJECT.md)
* [현재 아키텍처와 데이터 경계](docs/CURRENT_ARCHITECTURE.md)
* [시스템 개발 가이드라인](docs/SYSTEM_GUIDELINES.md)
* [워크스페이스 구성과 저장소 관리](docs/WORKSPACE.md)
* [클라이언트 모듈 안내](modules/ave-client/README.md)

## 저장소 구조

```text
ave-workspace/
├─ docs/                  # 여러 모듈이 공유하는 설계 문서
├─ modules/
│  ├─ ave-client/         # Windows 클라이언트
│  ├─ ave-server/         # 중앙 API 서버
│  ├─ ave-whisper-api/    # 원격 STT API
│  └─ ave-dist/           # 향후 배포 모듈
├─ AGENTS.md              # 워크스페이스 작업 원칙
└─ setup.bat              # 모듈 저장소 준비 스크립트
```

각 `modules/<모듈>`은 독립된 Git 저장소입니다. 모듈별 변경은 해당 저장소에서 관리하고, 여러 모듈에 공통으로 영향을 주는 문서만 루트 `docs/`에 기록합니다.

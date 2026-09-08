# AVE Client

AVE Client는 사용자의 Windows PC에서 영상 편집을 수행하는 클라이언트 모듈이다. 원본 영상과 렌더링 결과는 로컬 `media/`에만 보관한다.

## 책임

* React 웹 UI와 로컬 FastAPI 서버 실행
* yt-dlp로 YouTube 메타데이터·영상·댓글·채팅·자막·캡션 수집
* FFmpeg로 구간 편집, 음원 추출, 자막 합성, 결과 렌더링
* AVE Server를 통한 로그인 검증, LLM·원격 Whisper 호출, 작업 이력 동기화
* 완료된 결과만 로컬 SQLite 및 AVE Server 이력에 동기화
* 시스템 트레이에서 UI 열기·독립 클라이언트 로그 GUI 확인·종료 제공. 로그는 파일에 보존하는 동시에 메모리 링 버퍼를 통해 GUI에 전달

클라이언트는 원본 영상, 렌더링 영상, 로컬 경로, 서버 API 키, Supabase 서비스 키를 서버로 보내지 않는다.

## 요구 환경

* Windows x64
* Python 3.11 이상
* AVE Server URL
* `/bin/yt-dlp.exe`, `/bin/ffmpeg.exe`, `/bin/ffprobe.exe`

개발 환경에서 영상 도구를 받으려면 다음을 실행한다.

```powershell
powershell.exe -ExecutionPolicy Bypass -File .\scripts\fetch-tools.ps1
```

도구 배치·업데이트·배포 전 확인 항목은 [docs/TOOLCHAIN.md](docs/TOOLCHAIN.md)를 참고한다.

## 설정

`.env.example`을 복사해 `.env`를 만들고 AVE Server 주소만 설정한다.

```env
AVE_SERVER_URL=https://ave-server.example.com
WHISPER_HEARTBEAT_SECONDS=10
```

`YTDLP_COOKIES_FROM_BROWSER`, `YTDLP_COOKIEFILE`, `YTDLP_PLAYER_CLIENT`, `YTDLP_FORMAT`은 YouTube 수집 문제를 조정할 때만 선택적으로 사용한다.

Supabase 공개 로그인 설정과 LLM·Whisper 자격 증명은 AVE Server가 관리한다. 클라이언트 `.env`에 저장하지 않는다.

기본 LLM은 DeepSeek(`deepseek-chat`)이며 UI·API·Gateway의 기본값이 같다. Gemini도 같은 JSON 응답 계약과 공통 Gateway로 선택할 수 있다. 병렬 정책은 공급자별로 분리한다. DeepSeek는 작업당 최대 100개의 독립 요청을 병렬 실행하고, Gemini는 15 RPM 한도에 맞춰 한 번에 하나만 제출하며 요청 시작 사이를 4초 이상 둔다. 여러 클라이언트에 걸친 계정 전체 RPM은 AVE Server가 추가로 제한해야 한다. 공급자 오류는 분석을 중단하고 원인을 화면에 표시한다. Whisper 초기 프롬프트와 핫워드는 기본적으로 비활성화한다. 초기 프롬프트를 켜면 LLM을 사용하지 않고 선택 언어별 내용 중립적인 문장부호 예시를 고정 적용하며, 자동 언어 감지에서는 언어 편향을 피하기 위해 비워 둔다. 핫워드를 켜는 시점에만 LLM을 한 번 호출하여 메타데이터 원문에 실제 존재하며 특정 대상을 식별하는 고유명사를 개체 유형과 함께 판정하고, 검증을 통과한 항목을 최대 10개·항목당 40자로 입력한다. 각 옵션을 끄면 입력란을 숨기고 전사 요청에도 보내지 않는다. 사용자가 핫워드를 수정한 뒤 `초기화`를 누르면 최초 자동 생성값으로 되돌린다.

렌더링 자동 모드는 FFmpeg가 포함한 `h264_nvenc`(NVIDIA), `h264_amf`(AMD), `h264_qsv`(Intel) H.264 인코더를 이 순서로 실제 렌더링에 시도한다. 한 GPU가 드라이버·장치·필터 호환성으로 실패하면 남은 GPU 후보를 계속 시도하고, 모두 실패한 경우에만 CPU `libx264`로 처음부터 다시 렌더링한다. GPU 인코더를 선택한 영상 재인코딩은 입력에도 `-hwaccel auto`를 적용해 하드웨어 디코딩을 우선 시도한다. 자막 합성·구간 trim처럼 CPU 필터가 필요한 단계는 FFmpeg가 안전하게 프레임을 전송해 처리한다. 렌더링 진행 메시지와 완료 메시지에는 실제로 사용한 GPU 또는 CPU 방식을 표시한다. `AVE_VIDEO_ENCODER=cpu`로 GPU를 끌 수 있고, 특정 인코더 이름을 지정하면 해당 인코더만 시도한 뒤 CPU로 전환한다. 오디오 추출과 스트림 복사는 영상 재인코딩이 아니므로 하드웨어 가속 대상이 아니다.

이미 종료된 작업에 브라우저가 뒤늦게 취소를 요청해도 취소 API는 성공으로 응답한다. `yt-edit/{job_id}`의 작업 파일은 취소·실패 뒤에도 자동 삭제하지 않는다.

렌더링 등 실행 중 오류는 SSE로 원인을 한 번 전달한 뒤 작업 폴더·메모리·토큰을 정리한다. SSE 단절로 이 마지막 메시지를 받지 못한 경우 UI는 상태 조회의 404를 종료·정리됨으로 표시한다.

4단계 렌더링은 모든 SRT·구간 결합 영상·인코더별 시도 영상·pending 영상을 `yt-edit/{job_id}` 안에 고유한 이름으로 생성하고 성공·실패와 관계없이 보존한다. 완성된 pending 영상은 별도의 같은 디렉터리 파일로 복사한 뒤 `os.replace`로 최종 영상을 원자적으로 교체한다. 기본 번인 자막은 Dotum 16, 하단 여백 12, 한 줄 30자 줄바꿈을 사용하며 0.08초 미만으로 표시되는 자막은 제외한다. YouTube 롤링 캡션의 완료 표식은 직전 완료 표식부터 현재 완료 시점까지의 실제 표시 구간으로 복원한 뒤 분석과 렌더링에 사용한다. FFmpeg 자막에는 드라이브·경로 구분자를 이스케이프한 절대 경로를 전달하며 Windows 임시 폴더는 사용하지 않는다. 렌더링이 성공하면 서버 이력 동기화가 실패해도 결과 영상과 로컬 완료 이력은 유지하며, UI에는 서버가 반환한 동기화 실패 원인을 표시한다.

하단 진행 메시지는 긴 로그가 화면 밖으로 넘치지 않도록 끝부분을 흐리게 표시한다. 메시지를 세 번 클릭하면 브라우저의 문단 선택으로 숨겨진 부분을 포함한 전체 로그를 선택·복사할 수 있다.

## 장시간 작업 추적과 취소

작업 ID는 1단계에서 2단계로 넘어가며 추가 자료 다운로드를 시작할 때 한 번 확정하고 `<video_id>.<unix_timestamp_hex>` 형식을 사용한다. 이후 추가 자료 재수집, Whisper 재수행, 분석과 렌더링은 모두 같은 ID를 사용한다. 진행 중 상태는 프로세스 메모리에만 존재하며 새로고침·프로세스 재시작·취소·일반 실패 시 복원하지 않는다. `yt-edit/{job_id}`의 작업 파일은 취소·실패·중단 뒤에도 보존한다. LLM 응답이 20회 재시도 후에도 계약을 충족하지 못한 경우에는 확률적 오류로 보고 작업을 일시중지하며, 진행 도크의 기존 `작업 취소` 자리를 `재시도` 버튼으로 교체한다. 사용자가 재시도하면 저장된 LLM 체크포인트를 재사용한다. 결과 영상 생성이 성공한 작업만 SQLite와 AVE Server 이력에 저장한다.

완료 작업은 활성 작업 복구 대상이 아니다. 결과 조회 화면에서 **처음**을 누르면 메타데이터 단계로 돌아가며, 새 편집은 새 작업 ID로 시작한다.

## 로컬 파일 경계

파일 경로는 생성 시점이나 임시 여부가 아니라 내용이 결정되는 단위를 기준으로 선택한다.

* 한 영상에서 항상 동일한 원본 자료: `yt-data/{video_id}`
* 고정된 처리로 생성되어 같은 영상에서 항상 동일한 파생 자료: `yt-edit/{video_id}`
* 작업 설정·외부 응답·사용자 선택·실행 결과에 따라 달라질 수 있는 자료: `yt-edit/{job_id}`

`yt-data/{video_id}`는 yt-dlp가 만든 원본만 보관하며 재작업 때 그대로 재사용한다. `{video_id}.info.json`은 yt-dlp 저장 직후와 기존 캐시 재사용 시점에 댓글 본문 `comments`를 제외하고 UTF-8·2칸 들여쓰기 형식으로 원자적 재저장한다. 댓글 개수와 자막·캡션 트랙 목록 같은 공개 메타데이터는 유지하며, 실제 댓글 본문은 사용자가 댓글 자료를 선택한 경우에만 별도 파일로 수집한다. `yt-edit/{video_id}`는 2단계에서 원본으로부터 만든 재사용 가능한 자막·캡션 및 메타데이터 자료이고, 완료·실패와 관계없이 보존한다. 작업별 `yt-edit/{job_id}`에는 Whisper 전사, 분석 스크립트·계획·LLM 체크포인트와 최종 영상을 둔다.

| 위치 | 파일 | 역할 |
| --- | --- | --- |
| `yt-data/{video_id}` | `{video_id}.info.json`, 댓글 JSON, 채팅 JSONL, 자막·캡션 VTT, `{video_id}.mp3` | yt-dlp 원본과 최고 품질 공용 오디오 |
| `yt-edit/{video_id}` | `{video_id}.comments-timestamps.json`, `{video_id}.chat-times.json`, `{video_id}.whisper.mp3` | 분석·전사용 파생 메타데이터 |
| `yt-edit/{video_id}` | `{video_id}.{lang}.captions-rolling.vtt`, `*.{subtitles,captions}-transcript.json` | 표시용 롤링 캡션과 파싱 스크립트 |
| `yt-edit/{job_id}` | Whisper 전사, 분석 계획·LLM 체크포인트, SRT·결합·인코더 시도·pending 영상, `{job_id}.edited.mp4` | 작업별 자료와 산출물 |

`{video_id}.mp3`는 yt-dlp의 `bestaudio/best` 선택과 최고 MP3 품질 설정으로 별도 다운로드해 `yt-data/{video_id}`에 둔다. 영상 다운로드 형식은 기존 정책을 유지한다. 원본 MP3는 향후 음량 분석에 재사용하고, Whisper 전사에는 이를 16 kHz·모노·64 kbps로 고정 변환한 `yt-edit/{video_id}/{video_id}.whisper.mp3`를 사용한다. 두 MP3는 완료 후에도 삭제하지 않으며, 전사용 파일은 이미 있으면 다시 변환하지 않는다. Whisper 전사 결과는 언어·프롬프트·hotwords·배속과 원격 실행에 따라 달라질 수 있으므로 `{job_id}`에 저장한다.

3단계는 2단계에서 준비한 자료만 읽으며 yt-dlp 재수집이나 VTT 재파싱을 하지 않는다. 선택한 자막·캡션 언어의 전사 파일만 정확히 읽으므로, 같은 영상에 여러 언어 자료가 있어도 이전에 선택한 언어가 섞이지 않는다. Whisper를 선택한 경우에는 같은 `job_id`의 작업 폴더에서 전사를 읽는다. 전체 스크립트는 한 번의 LLM 호출로 `summary`와 0~1000 정수 `score`가 있는 챕터로 나누고, 각 챕터는 시간 범위만 있는 섹션으로 분할한다. 중요도 평가는 챕터별 summary와 섹션 `[{"id","text"}]` 배열을 한 번에 보내고 같은 ID의 `[{"id","score"}]` 배열만 받는다. JSON 문법·ID·범위 계약이 틀리면 추측해 보정하지 않고 최대 20회 재시도한다. 재시도 소진 시 작업을 일시중지하고 명시적 재개 때 체크포인트를 사용한다. 독립 요청은 공급자별 제한을 적용한다. DeepSeek는 작업당 최대 100개를 병렬 실행하고, Gemini는 15 RPM에 맞춰 한 번에 하나씩 요청하며 시작 간격을 4초 이상 둔다. 3단계 소스는 지원되는 자막, 캡션, Whisper 순서로 제공하며, 초기 목표 길이는 영상 전체 길이의 1/4(최대 7,200초)다. 4단계는 챕터 → 섹션 계층에서 챕터·섹션의 실제 LLM 점수 배지, 본문과 선택 상태를 보여 준다. 전사·분석 JSON은 같은 디렉터리의 임시 파일에 완전히 쓴 뒤 `os.replace`로 교체한다. 채팅 밀도·댓글 타임스탬프·히트맵·음량을 조합한 점수와 기승전결 탐색 및 채팅 밀도 표시는 지원 예정이며 현재는 사용하지 않는다.

자막·캡션 파싱 파일과 Whisper 전사 파일은 모두 `{"segments":[{"start":number,"end":number,"text":string}]}` 형식이다. 영상에 업로드 자막과 자동 캡션이 없거나, 제공되더라도 사용자가 두 자료를 모두 선택 해제하면 Whisper STT를 자동 활성화하고 스크립트 소스를 `whisper_api`로 고정한다. 자막이나 캡션 중 하나를 다시 선택해야 Whisper를 선택적으로 끌 수 있다. 분석 시작 API도 영상 자체에 두 스크립트가 없는 조건을 다시 확인한다. Whisper 전사 배속은 `1.0 (품질)`, `1.5 (균형)`, `2.0 (속도)`만 선택할 수 있으며 기본값은 품질이다. YouTube 챕터는 설명의 타임스탬프 문구를 파싱하거나 번역해 만들지 않는다. 한국어 로케일(`hl=ko`, `gl=KR`)의 YouTube 페이지가 플레이어용 `DESCRIPTION_CHAPTERS` 데이터로 직접 제공한 제목과 시각을 우선 저장하고 UI에 표시한다.

Whisper API는 WhisperX `large-v3` 전사 후 언어별 CTC 음향 모델로 한 번 강제 정렬하여 세그먼트와 그 안의 단어 타임스탬프를 함께 반환한다. 클라이언트는 `engine=whisperx-aligned-word-v1`, `alignment=ctc-forced-alignment-with-words`를 검증한다. 공백 제외 25자 이하 세그먼트는 원래 세그먼트 시각을 그대로 사용한다. 이를 초과한 세그먼트는 먼저 단어 사이 0.5초 이상 무음 경계에서 나누고, 앞 자막의 종료는 앞 단어 `end`, 뒤 자막의 시작은 다음 단어 `start`로 정한다. 이렇게 나눈 조각이 여전히 25자를 초과하면 LLM에 분할 경계 단어 인덱스를 요청한다. 목표 구간이 N개라면 LLM은 각 앞 구간의 마지막 단어 인덱스 N-1개만 오름차순으로 반환하고, 클라이언트가 누락·중복 없는 연속 단어 범위를 구성한다. LLM 경계는 이전 단어 `end`와 다음 단어 `start` 사이의 중간값으로 정해 앞뒤 자막이 같은 경계에서 맞닿게 한다. 원본 세그먼트의 첫 시작과 마지막 종료는 보존한다. 원본 API 결과는 `{job_id}.whisper-source.json`에 보존하며 `timestamp_mode=whisperx-aligned-word-v1` 캐시 지문으로 이전 결과의 재사용을 차단한다.

Whisper 전사 중에는 `WHISPER_HEARTBEAT_SECONDS`마다 AVE Server에 heartbeat를 보낸다. 브라우저는 각 상태 조회 전에 현재 Supabase 세션을 확인하고 만료 60초 전부터 토큰을 갱신하며, 401 응답에는 강제 갱신 후 한 번 재시도한다. 인증된 상태 조회에 사용된 최신 토큰은 같은 사용자의 로컬 백그라운드 작업에 전달되어 이후 heartbeat·SSE 재연결·완료 ACK·후속 LLM 요청에 사용된다. 클라이언트는 세그먼트 결과를 성공적으로 저장한 뒤 ACK를 보내며, ACK가 네트워크 오류로 실패해도 서버는 기본 15분 동안 완료 결과를 보관해 SSE 재연결이 다시 받을 수 있게 한다. ACK 또는 TTL 만료 뒤 전사 제어 레코드는 삭제된다. 로컬 클라이언트 자체가 종료되거나 네트워크가 끊기면 heartbeat가 멈추며, 서버의 lease 만료 정책이 RunPod 작업·임시 MP3·실행 제어 레코드를 정리한다. 사용자가 UI의 **작업 취소**를 누르면 전사 준비 단계에서도 `client_job_id` 기준 취소 의도를 서버에 먼저 전달한다. 서버는 RunPod 작업 ID가 아직 없더라도 이를 확인해 요청을 건너뛰거나 즉시 취소한다. 이 제어 정보는 완료 이력이 아니며 처리 또는 만료 뒤 삭제된다. 다운로드·FFmpeg·전사처럼 동기 실행 중인 로컬 구간은 다음 진행도 보고 지점에서 협력적으로 취소된다.

## 실행

의존성을 설치한 뒤 실행한다.

```powershell
python -m pip install -r requirements.txt
python -m app
```

`python -m app`은 로컬 FastAPI 서버를 백그라운드에서 실행하고 시스템 트레이 아이콘과 기본 React UI를 연다. 트레이에서 종료를 선택하면 진행 중인 작업 취소 여부를 확인하며, 확인 시 로컬 서버·AVE Server·RunPod에 순서대로 취소를 요청한 뒤 종료한다.

향후 사용자 배포는 pynsist 설치 프로그램이 아닌 PyInstaller 기반 Windows 포터블 앱을 사용한다. 현재는 패키징을 구현하지 않았으며 PyInstaller 설정과 산출물은 추후 `ave-dist`에서 관리한다.

개발 중 UI를 수정했다면 다음 명령으로 빌드한다.

```powershell
cd ui
npm install
npm run build
```

WSL 1에서는 Windows Node.js 설치 경로를 확인할 수 없어 `npm run build`가 실행되지 않는다. 이 경우 Windows PowerShell에서 워크스페이스를 연 뒤 아래 명령으로 Python 테스트와 UI 빌드를 실행한다.

```powershell
cd modules\ave-client
.\.venv\Scripts\python.exe -m pytest -q
cd ui
npm run build
```

## 주요 API

* `POST /api/youtube/metadata`: 영상 메타데이터 조회
* `POST /api/youtube/metadata/materials/start`, `GET /api/youtube/metadata/materials/{job_id}`: 2단계 자료 준비와 일회성 완료 응답 조회
* `POST /api/youtube/edit/start`: 분석 작업 시작
* `GET /api/youtube/edit/{job_id}/events`: 분석·렌더링 진행 상태 SSE 스트림
* `GET /api/youtube/edit/status/{job_id}`: SSE 재연결 전 상태 확인
* `GET /api/youtube/edit/active`, `POST /api/youtube/edit/{job_id}/cancel`: 진행 작업 복구와 단일 작업 취소
* `POST /api/youtube/edit/cancel-active`: 브라우저 확인 또는 트레이 종료 시 모든 활성 작업 취소
* `GET`/`PUT /api/youtube/edit/{job_id}/segments`: 추천 구간 조회·선택 렌더링
* `GET /api/youtube/edit/{job_id}/media/source`: 활성 선택·렌더링 작업의 원본 미리보기
* `GET /api/youtube/edit/{job_id}/media/rendered`: 완료된 결과 영상 조회

## 문서

* [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): 클라이언트와 서버 책임 경계
* [docs/TOOLCHAIN.md](docs/TOOLCHAIN.md): Windows 바이너리 관리
* [docs/UI_PARITY.md](docs/UI_PARITY.md): React UI 기능과 수동 확인 절차

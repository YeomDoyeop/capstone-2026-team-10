# ave-whisper-api

WhisperX 기반의 AVE 음성 전사·강제 정렬 API입니다. 이전 faster-whisper 전용 구현은 `legacy/faster-whisper-v1/`에 보존하며 신규 배포에는 사용하지 않습니다.

```text
src/                 입력 검증, 오디오 다운로드, 모델 전사 로직
deploy/runpod/       RunPod Serverless Queue worker 구성
tests/               입력 및 오디오 처리 단위 테스트
```

## API 계약

전사 입력은 다음 필드를 사용합니다.

```json
{
  "audio_url": "https://example.com/audio.mp3",
  "language": "ko",
  "initial_prompt": "AVE 프로젝트 회의",
  "hotwords": "AVE, 서울대학교",
  "speed": 1.0
}
```

`audio_url`만 필수이며, `language`의 기본값은 `ko`입니다. `speed`는 `1.0~2.0`만 허용합니다. 배속 전사 시에도 `duration`과 세그먼트 타임스탬프는 원본 오디오 시간축 기준으로 반환됩니다.

성공 시 다음 JSON을 반환합니다.

```json
{
  "text": "전체 전사문",
  "language": "ko",
  "duration": 12.345,
  "engine": "whisperx-aligned-word-v1",
  "alignment": "ctc-forced-alignment-with-words",
  "segments": [
    {
      "start": 0.0,
      "end": 1.0,
      "text": " 첫 문장",
      "words": [
        {"start": 0.0, "end": 0.4, "word": " 첫"},
        {"start": 0.4, "end": 1.0, "word": " 문장"}
      ]
    }
  ]
}
```

WhisperX의 `large-v3`, `float16`, Silero VAD로 한 번 전사한 뒤 감지 또는 지정된 언어의 CTC 음향 모델로 원본 전사문을 강제 정렬합니다. 한국어는 WhisperX 기본 매핑인 `kresnik/wav2vec2-large-xlsr-korean`을 사용합니다. 응답에는 정렬된 문장 세그먼트와 그 안의 단어 타임스탬프가 함께 포함되며 `engine`과 `alignment` 필드로 실제 worker 버전을 식별합니다.

## 입력 제약

입력 URL은 공개 `https` URL만 허용합니다. 로컬·사설 IP 주소, 기본 1 GiB를 초과하는 파일, 기본 10분을 초과하는 다운로드는 거부됩니다. 결과와 입력 파일은 작업 중인 임시 디렉터리에만 저장됩니다.

## 환경 변수

| 이름 | 기본값 | 설명 |
| --- | --- | --- |
| `MODEL_CACHE_DIR` | `/models` | WhisperX `large-v3` 모델 캐시 경로 |
| `ALIGN_MODEL_CACHE_DIR` | `/models/alignment` | 언어별 CTC 정렬 모델 캐시 경로 |
| `WHISPERX_BATCH_SIZE` | `16` | WhisperX 전사 배치 크기 |
| `MAX_DOWNLOAD_BYTES` | `1073741824` | 입력 오디오 최대 다운로드 크기 (1 GiB) |
| `DOWNLOAD_CONNECT_TIMEOUT_SECONDS` | `15` | URL 연결과 TLS 협상 제한 시간(초) |
| `DOWNLOAD_READ_TIMEOUT_SECONDS` | `60` | 다운로드 중 새 데이터 수신 제한 시간(초) |
| `DOWNLOAD_MAX_ATTEMPTS` | `3` | 네트워크 오류 발생 시 다운로드 재시도 횟수 |

## 테스트

GPU 없이 입력 검증과 오디오 처리 단위 테스트를 실행할 수 있습니다.

```powershell
python -m unittest discover -s tests -v
```

## RunPod 배포 절차

이 저장소의 RunPod worker는 `deploy/runpod/Dockerfile`과 `deploy/runpod/handler.py`를 사용합니다. HTTP 서버 포트를 직접 열지 않습니다. RunPod Serverless의 **Queue Endpoint**가 요청을 받아 `handler(job)`을 실행하는 구조입니다.

### 1. 사전 준비

다음 항목을 준비합니다.

* Docker Desktop (Windows에서는 Linux 컨테이너 모드)
* Docker Hub 계정 및 이미지 저장소 생성 권한
* 결제가 가능한 RunPod 계정과 API 키
* 테스트할 공개 HTTPS 오디오 URL

프로젝트 루트인 `ave-whisper-api` 디렉터리에서 Docker가 동작하는지 확인합니다.

```powershell
docker version
python -m unittest discover -s tests -v
```

### 2. Docker Hub 로그인 및 이미지 빌드

아래의 `<dockerhub-user>`를 본인의 Docker Hub 사용자명으로 바꿉니다. `<version>`에는 변경하지 않을 버전 태그를 사용합니다. WhisperX 계열의 첫 버전은 `v0.2.0`을 사용합니다.

```powershell
docker login

docker build --platform linux/amd64 `
  -f deploy/runpod/Dockerfile `
  -t <dockerhub-user>/ave-whisper-api:<version> .
```

`--platform linux/amd64`는 RunPod worker에서 실행할 Linux AMD64 이미지를 만들기 위한 옵션입니다. 빌드가 완료되면 Docker Hub에 올립니다.

```powershell
docker push <dockerhub-user>/ave-whisper-api:<version>
```

이미지가 공개 저장소가 아니라면 RunPod에서 해당 Docker Hub 저장소에 접근할 수 있도록 레지스트리 자격 증명을 함께 설정해야 합니다. 처음 검증할 때는 공개 저장소가 간단합니다. 이미지 태그는 `latest` 대신 `v0.2.0`처럼 고정된 값을 사용합니다.

### 3. RunPod Queue Endpoint 생성

1. RunPod 콘솔에서 **Serverless**로 이동한 뒤 **New Endpoint**를 선택합니다.
2. **Deploy from a Docker image**를 선택합니다.
3. Container image에 다음처럼 방금 올린 전체 이미지 이름을 입력합니다.

   ```text
   <dockerhub-user>/ave-whisper-api:<version>
   ```

4. Endpoint 유형으로 **Queue**를 선택합니다.
5. `float16`을 지원하는 NVIDIA GPU를 선택합니다. `large-v3`와 정렬 모델을 함께 올리므로 GPU 메모리 24 GB 이상인 인스턴스를 권장합니다.
6. 비용을 제한하려면 **Min Workers**를 `0`, **Max Workers**를 `1`로 설정합니다. 요청이 없을 때 worker가 내려가며, 첫 요청은 worker 시작과 모델 다운로드 때문에 오래 걸릴 수 있습니다.
7. 긴 오디오도 검사할 수 있도록 실행 제한 시간은 우선 `900`초로 설정합니다. 실제 서비스에서는 최대 오디오 길이에 맞춰 조정합니다.
8. Endpoint를 생성하고 worker 로그에서 이미지 시작 및 모델 다운로드가 완료되는지 확인합니다.

첫 worker 시작 시 `large-v3` 모델을 내려받고, 첫 언어 요청 시 해당 CTC 정렬 모델을 추가로 내려받아 적재합니다. 이 시간에는 요청이 대기 상태일 수 있으며 오류가 아닙니다.

### 4. RunPod 콘솔에서 첫 요청 보내기

Endpoint 상세 화면의 **Requests** 탭에서 다음 본문으로 요청합니다. RunPod 요청 본문에는 전사 입력을 `input` 객체로 감싸야 합니다.

```json
{
  "input": {
    "audio_url": "https://storage.googleapis.com/cloud-samples-data/speech/brooklyn_bridge.flac",
    "language": "en",
    "speed": 1.0
  }
}
```

완료된 응답의 `output` 안에 `text`, `language`, `duration`, `segments`가 있으면 배포가 정상입니다. 오류가 나면 `output.error.code` 및 `output.error.message`와 worker 로그를 함께 확인합니다.

### 5. PowerShell에서 비동기 요청 및 결과 확인

Endpoint ID는 RunPod 콘솔의 Endpoint 상세 화면에서 확인합니다. API 키는 브라우저 코드나 클라이언트 앱에 넣지 말고 서버 측 환경 변수로 관리합니다.

```powershell
$endpointId = "<RunPod endpoint ID>"
$apiKey = "<RunPod API key>"
$headers = @{ Authorization = "Bearer $apiKey" }
$body = @{
  input = @{
    audio_url = "https://storage.googleapis.com/cloud-samples-data/speech/brooklyn_bridge.flac"
    language = "en"
    speed = 1.0
  }
} | ConvertTo-Json -Depth 4

$job = Invoke-RestMethod `
  -Method Post `
  -Uri "https://api.runpod.ai/v2/$endpointId/run" `
  -Headers $headers `
  -ContentType "application/json" `
  -Body $body

$job
```

위 요청은 작업 ID를 즉시 반환합니다. `$job.id` 값을 사용해 상태와 결과를 조회합니다.

```powershell
Invoke-RestMethod `
  -Method Get `
  -Uri "https://api.runpod.ai/v2/$endpointId/status/$($job.id)" `
  -Headers $headers
```

응답의 `status`가 `COMPLETED`가 될 때까지 다시 조회합니다. 전사 결과는 `output`에 있습니다. worker는 오디오 다운로드, 배속 적용, 전사 구간 처리 단계마다 RunPod progress update를 기록하므로 상태 응답의 `progress`에서 진행률과 메시지를 확인할 수 있습니다. 짧은 작업만 즉시 결과가 필요한 경우 `/run` 대신 `/runsync`를 사용할 수 있지만, 모델 기동 시간을 고려하면 일반 전사 요청에는 `/run`과 상태 조회 방식을 권장합니다.

AVE Server는 사용자의 명시 취소 또는 heartbeat lease 만료 시 RunPod의 `POST /cancel/{job_id}`를 호출합니다. worker 내부의 동기 다운로드·전사 호출은 해당 호출 중간에 즉시 중단되지 않을 수 있으므로, 취소 후에는 RunPod 상태와 AVE Server의 최종 상태를 함께 확인해야 합니다.

### 6. 오류 확인 순서

1. RunPod Endpoint의 worker 로그에 모델 다운로드, CUDA 또는 메모리 부족 오류가 있는지 확인합니다.
2. 선택한 GPU가 `float16`을 지원하는지 확인합니다.
3. `audio_url`이 인증 없이 접근 가능한 공개 `https` URL인지 확인합니다.
4. 오디오 파일 크기와 다운로드 시간이 환경 변수 제한을 넘지 않는지 확인합니다.
5. 요청 본문이 반드시 `{ "input": { ... } }` 구조인지 확인합니다.

### 7. 새 버전 배포

코드를 바꾼 뒤에는 기존 태그를 덮어쓰지 말고 새 태그로 빌드·push합니다.

```powershell
docker build --platform linux/amd64 `
  -f deploy/runpod/Dockerfile `
  -t <dockerhub-user>/ave-whisper-api:v0.2.1 .

docker push <dockerhub-user>/ave-whisper-api:v0.2.1
```

RunPod Endpoint 설정에서 이미지 태그를 `v0.2.1`로 바꾸어 worker를 재배포한 뒤, 위와 같은 요청으로 다시 확인합니다. 문제가 생기면 이전에 검증된 이미지 태그로 되돌릴 수 있습니다.

RunPod 콘솔 절차와 API 형식은 [RunPod worker 배포 문서](https://docs.runpod.io/serverless/workers/deploy), [Endpoint 개요](https://docs.runpod.io/serverless/endpoints/overview), [요청 전송 문서](https://docs.runpod.io/serverless/endpoints/send-requests)를 기준으로 합니다.

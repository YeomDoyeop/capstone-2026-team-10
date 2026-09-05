# 분석 작업 API 계약

모든 `/api` 요청은 Supabase 사용자 액세스 토큰을 `Authorization: Bearer <token>` 헤더로 전달한다. 서버는 토큰의 사용자와 작업 소유자가 일치할 때만 데이터를 읽거나 수정한다.

## 작업 생성

`POST /api/analysis-jobs`는 렌더링 완료 뒤에만 클라이언트 로컬 작업에 대응하는 서버 이력을 만든다. 서버는 항상 `status: "completed"`, `progress: 100`으로 기록한다.

```json
{
  "client_job_id": "local-job-001",
  "source_id": "YouTube-영상-ID",
  "source_url": "https://www.youtube.com/watch?v=...",
  "title": "영상 제목",
  "duration_ms": 3600000
}
```

영상 파일이나 로컬 경로는 포함하지 않는다.

## 분석 결과 저장

`PUT /api/analysis-jobs/{job_id}/result`는 완료 이력에 추천 기준 개선용 편집 판단 데이터를 저장한다. 진행·실패·취소 상태를 생성하거나 기록하는 API는 제공하지 않는다.

```json
{
  "script": "선택 사항인 전체 스크립트",
  "segments": [
    {
      "segment_index": 0,
      "start_ms": 0,
      "end_ms": 30000,
      "text": "구간 스크립트",
      "script_importance": 0.82,
      "chat_density": 1.4,
      "comment_timestamp_count": 5,
      "heatmap_score": 0.73,
      "average_volume_dbfs": -18.2,
      "final_score": 0.79,
      "recommended": true
    }
  ],
  "heatmap": [],
  "recommendation": {},
  "selection": {}
}
```

`recommendation`에는 모델·규칙 버전과 추천 구간을, `selection`에는 사용자 선택·제외·수정·피드백 이력을 저장한다. 원본 영상, 렌더링 결과, 로컬 절대 경로는 저장하지 않는다.

## 서버 LLM 호출

`POST /api/llm/generate`는 클라이언트가 직접 공급자 API 키를 보관하지 않도록 Gemini와 DeepSeek 호출을 서버에서 수행한다. `provider`를 생략하면 기본값은 DeepSeek다.

```json
{
  "provider": "deepseek",
  "model": "선택 모델명",
  "system": "시스템 지시문",
  "prompt": "분석 요청",
  "response_schema": {}
}
```

서버 `.env`의 `GEMINI_API_KEY` 또는 `DEEPSEEK_API_KEY`가 필요하다.

공급자가 HTTP 429를 반환하면 서버는 사용량 한도 도달 원인과 함께 503을, HTTP 503을 반환하면 무료 사용량 한도 또는 공급자 일시 장애 가능성과 함께 503을 반환한다. 클라이언트는 이 응답이 세 번 연속 발생하면 분석을 중단한다.

## 원격 Whisper 전사

클라이언트는 `POST /api/stt-files`의 multipart `file` 필드에 FFmpeg로 추출한 MP3를 보낸다. 응답의 `file_id`를 `POST /api/stt/transcriptions`에 전달하면 서버가 RunPod의 `ave-whisper-api`를 호출한다. 진행도 조회를 사용하는 클라이언트는 `track_progress: true`와 로컬 작업 ID인 `client_job_id`, 서버 분석 이력 ID인 `server_job_id`를 포함한다. 서버는 이 ID 연결, RunPod 작업 ID, 임시 파일 ID, heartbeat/lease를 `transcription_jobs`에 실행 제어용으로만 보관하고, 종료 뒤 삭제한다. 이는 완료 이력이 아니다.

`GET /api/stt/transcriptions/{job_id}/events`는 `text/event-stream`으로 RunPod worker가 보고한 `progress`(0~100)와 상태 메시지를 `status` 이벤트로 전달한다. AVE Server는 이 스트림을 생성하는 동안 RunPod에는 REST 상태 조회를 사용한다. `GET /api/stt/transcriptions/{job_id}`는 진단·호환용 REST 상태 조회로 유지한다. 로컬 클라이언트는 `POST /api/stt/transcriptions/{job_id}/heartbeat`를 10초마다 호출해 lease를 연장한다. 서버는 `TRANSCRIPTION_LEASE_SECONDS`(기본 60초) 동안 heartbeat가 없으면 RunPod 작업을 취소하고 임시 MP3를 삭제한다. 만료 후 도착한 상태 조회·heartbeat는 lease를 다시 연장하지 않고 즉시 취소 정리를 시작하며, 별도 sweep도 `TRANSCRIPTION_LEASE_SWEEP_SECONDS`마다 같은 정리를 수행한다.

`POST /api/stt/transcriptions/{job_id}/cancel`은 소유자가 RunPod 작업 ID를 받은 뒤 명시적으로 작업을 취소하는 API다. 취소 요청은 RunPod `/cancel`로 전파된다. RunPod 작업 ID 전에는 `POST /api/stt/transcriptions/client/{client_job_id}/cancel`을 사용한다. 이 API는 10분 TTL의 취소 의도를 기록하며 서버는 RunPod 호출 전·후 이를 확인해 호출을 생략하거나 즉시 취소한다. 전사 결과 `result`는 완료를 처음 감지한 `GET /api/stt/transcriptions/{job_id}` 응답에 포함된다.

전사 작업 상태는 `queued → in_progress → completed|failed|cancelled`로 전이한다. 취소 요청 중 RunPod 응답을 기다리는 상태는 `cancel_requested`이며, 서버 재시작 뒤에도 `transcription_jobs`에서 이어서 정리한다. terminal 상태는 응답 직후 삭제되고, heartbeat가 사라진 작업은 lease sweep이 취소·삭제한다. 시작되지 않은 취소 의도도 TTL 만료 뒤 삭제된다.

전사 요청 성공·실패와 무관하게 서버는 해당 임시 MP3를 삭제한다. `/files/` URL은 RunPod worker의 일시적 다운로드 용도이며, 클라이언트가 직접 SFTP로 접근하지 않는다.

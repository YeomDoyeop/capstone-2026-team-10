# 분석 작업 API 계약

모든 `/api` 요청은 Supabase 사용자 액세스 토큰을 `Authorization: Bearer <token>` 헤더로 전달한다. 서버는 토큰의 사용자와 작업 소유자가 일치할 때만 데이터를 읽거나 수정한다.

## 작업 생성

`POST /api/analysis-jobs`는 클라이언트 로컬 작업에 대응하는 서버 기록을 만든다.

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

## 작업 상태

`PATCH /api/analysis-jobs/{job_id}`로 클라이언트의 로컬 처리 상태를 기록한다.

```json
{
  "status": "analyzing",
  "progress": 60,
  "error_message": null
}
```

상태 값은 `queued`, `collecting`, `analyzing`, `rendering`, `completed`, `failed`이다.

## 분석 결과 저장

`PUT /api/analysis-jobs/{job_id}/result`는 추천 기준 개선에 사용할 편집 판단 데이터를 저장한다.

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

`POST /api/llm/generate`는 클라이언트가 직접 공급자 API 키를 보관하지 않도록 Gemini와 DeepSeek 호출을 서버에서 수행한다.

```json
{
  "provider": "gemini",
  "model": "선택 모델명",
  "system": "시스템 지시문",
  "prompt": "분석 요청",
  "response_schema": {}
}
```

서버 `.env`의 `GEMINI_API_KEY` 또는 `DEEPSEEK_API_KEY`가 필요하다.

## 원격 Whisper 전사

클라이언트는 `POST /api/stt-files`의 multipart `file` 필드에 FFmpeg로 추출한 MP3를 보낸다. 응답의 `file_id`를 `POST /api/stt/transcriptions`에 전달하면 서버가 RunPod의 `ave-whisper-api`를 호출한다.

전사 요청 성공·실패와 무관하게 서버는 해당 임시 MP3를 삭제한다. `/files/` URL은 RunPod worker의 일시적 다운로드 용도이며, 클라이언트가 직접 SFTP로 접근하지 않는다.

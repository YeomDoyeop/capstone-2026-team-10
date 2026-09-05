# AVE 서버 모듈

`ave-server`는 AVE의 중앙 API 서버이다. 사용자 인증, 작업 이력, 편집 판단 데이터, 외부 API 호출을 담당한다.

원본 영상과 렌더링 결과 영상은 서버에 저장하지 않는다. 클라이언트가 로컬에서 처리한 뒤 스크립트, 구간별 분석값, 추천 결과와 사용자 선택 이력만 서버로 전송한다.

원격 STT가 필요한 경우에만 서버가 전사용 오디오를 임시로 호스팅하고 `ave-whisper-api`를 호출한다. 임시 오디오는 전사 뒤 삭제한다.

## 1단계 API

현재 분리 단계에서는 다음 API를 먼저 제공한다.

* 인증 사용자 확인
* 브라우저 로그인용 공개 Supabase 구성 제공
* 분석 작업 생성·상태 갱신·조회
* 스크립트, 구간 분석값, 추천·선택 결과 저장·조회

API 세부 계약과 DB 스키마는 `docs/`에서 관리한다.

## API와 데이터 흐름

클라이언트는 Supabase 액세스 토큰을 포함해 서버 API를 호출한다. 서버는 토큰의 사용자와 작업 소유자를 검증한 뒤 처리한다.

* `POST /api/analysis-jobs`: 로컬 편집 작업의 서버 기록 생성
* `GET /api/auth/config`: 브라우저 Google 로그인에 필요한 Supabase URL·anon 공개 키 반환
* `POST /api/analysis-jobs`: 렌더링 완료 뒤 완료 이력 생성
* `PUT /api/analysis-jobs/{job_id}/result`: 완료된 작업의 구간별 분석값과 추천·선택 정보 저장
* `POST /api/llm/generate`: Gemini 또는 DeepSeek API 호출
* `POST /api/stt-files`, `POST /api/stt/transcriptions`: 임시 MP3 업로드와 lease 기반 원격 Whisper 전사
* `POST /api/stt/transcriptions/{job_id}/heartbeat`, `POST /api/stt/transcriptions/{job_id}/cancel`: heartbeat와 RunPod 작업 ID 기반 취소 전파
* `POST /api/stt/transcriptions/{job_id}/ack`: 클라이언트의 Whisper 결과 파일 저장 확인
* `POST /api/stt/transcriptions/client/{client_job_id}/cancel`: RunPod 작업 ID 반환 전의 전사 준비 단계 취소 의도 등록

원본·렌더링 영상과 클라이언트 로컬 경로는 이 API로 전송하지 않는다. Whisper용 MP3는 전사 요청이 끝나면 서버가 삭제한다.

`transcription_jobs`는 완료 이력이 아닌 실행 제어용 임시 레코드다. 로컬 클라이언트 heartbeat가 `TRANSCRIPTION_LEASE_SECONDS`(기본 60초) 동안 끊기면 서버가 RunPod 작업을 취소하고 임시 MP3와 제어 레코드를 정리한다. 실패·취소는 즉시 삭제한다. 정상 완료 결과는 클라이언트가 `whisper-transcript.json` 저장을 끝내고 ACK를 보낼 때까지, 또는 `TRANSCRIPTION_RESULT_TTL_SECONDS`(기본 900초)가 만료될 때까지에 한해 재연결용으로 보관한다. ACK·만료 뒤에는 제어 레코드를 삭제하며 완료 이력으로 남기지 않는다. `TRANSCRIPTION_LEASE_SWEEP_SECONDS`(기본 15초)는 만료 정리 주기다. 전사 준비 중 취소는 `transcription_cancel_requests`에 최대 10분 동안만 기록하고, 서버는 RunPod 호출 전·후 이를 확인해 호출을 건너뛰거나 즉시 취소한다. 처리·만료된 취소 의도는 삭제한다. 완료된 분석 결과만 `analysis_jobs`·`analysis_results`에 보관한다. 배포 전에 갱신된 `docs/supabase_schema.sql`을 적용해야 한다.

## Ubuntu VM 배포

Ubuntu VM과 `ave-server.duckdns.org` 도메인을 사용하는 배포·실행 절차는 [배포 안내](deploy/README.md)를 따른다. 배포 구성은 API와 원격 STT용 임시 파일 호스팅을 함께 실행한다.

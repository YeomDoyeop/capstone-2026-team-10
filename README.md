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
* `PATCH /api/analysis-jobs/{job_id}`: 로컬 수집·분석·렌더링 상태 기록
* `PUT /api/analysis-jobs/{job_id}/result`: 구간별 스크립트 중요도, 채팅 밀도, 댓글 타임스탬프 집계, 히트맵, 음향 분석값, 추천·선택 이력 저장
* `POST /api/llm/generate`: Gemini 또는 DeepSeek API 호출
* `POST /api/stt-files`, `POST /api/stt/transcriptions`: 임시 MP3 업로드와 원격 Whisper 전사

원본·렌더링 영상과 클라이언트 로컬 경로는 이 API로 전송하지 않는다. Whisper용 MP3는 전사 요청이 끝나면 서버가 삭제한다.

## Ubuntu VM 배포

Ubuntu VM과 `ave-server.duckdns.org` 도메인을 사용하는 배포·실행 절차는 [배포 안내](deploy/README.md)를 따른다. 배포 구성은 API와 원격 STT용 임시 파일 호스팅을 함께 실행한다.

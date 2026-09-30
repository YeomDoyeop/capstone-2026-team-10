# 클라이언트 전환 구조

## 목표 기술 스택

* 로컬 엔진: Python, FastAPI, SQLite, 독립 실행형 `yt-dlp`, FFmpeg, FFprobe
* 사용자 인터페이스: React, TypeScript, Tailwind CSS, Vite
* 실행 방식: `python -m app`이 로컬 엔진과 시스템 트레이를 실행하고, Vite로 빌드한 UI를 제공한다.

## 책임 경계

클라이언트는 원본·결과 영상의 로컬 처리와 사용자 인터페이스를 담당한다. 서버에는 인증된 HTTP API로 작업 상태와 편집 판단 데이터만 전송한다.

* 로컬 전용: 영상 다운로드, 자막·채팅·히트맵·음향 분석, FFmpeg 렌더링, 로컬 미리보기
* 서버 전송: 렌더링 완료 작업의 현재 API 계약상 스크립트·구간 분석값·추천·선택 정보
* 서버 호출: 인증 검증, LLM, 원격 Whisper, 작업 기록

클라이언트 환경 파일에는 Supabase URL·키를 포함해 인증 비밀값, LLM 키·모델, RunPod 키·엔드포인트를 보관하지 않는다. 서버 `.env`만이 이 값을 소유하고, 로그인에 필요한 공개 구성은 서버의 `/api/auth/config`으로 제공한다.

## 전환 순서

1. React·TypeScript·Tailwind·Vite UI 프로젝트를 추가하고 현재 정적 UI를 기능 단위로 옮긴다.
2. UI가 Supabase 세션 토큰을 로컬 엔진에 전달한다.
3. 로컬 엔진이 서버 작업을 만들고, 분석·렌더링 상태와 결과를 서버 API에 동기화한다.
4. LLM 호출을 서버 API로 전환하고 클라이언트 환경 변수에서 LLM·RunPod 비밀값을 제거한다.
5. 사용자 구간 선택과 재렌더링 이력을 서버 작업에 기록한다.
6. 기존 정적 UI와 클라이언트 직접 Supabase Storage 업로드 코드를 제거한다. (기존 import API는 제거됨)

각 단계는 기존 로컬 영상 처리와 웹 UI 사용 흐름을 유지한 상태에서 검증한다.

## 현재 전환 상태

Vite 프로젝트는 `ui/`에 있으며 `npm run build` 결과를 `static/ui/`에 생성한다. 로컬 FastAPI 서버는 빌드 결과를 `/`와 `/ui/`에서 제공한다. 명령(시작·선택 렌더링·취소·heartbeat)은 REST로 처리하고, 브라우저의 로컬 편집 진행도는 `GET /api/youtube/edit/{job_id}/events` SSE로 전달한다. 원격 Whisper 진행도는 AVE Server의 `GET /api/stt/transcriptions/{job_id}/events` SSE를 로컬 엔진이 구독해 로컬 SSE로 중계한다. AVE Server와 LLM·RunPod Whisper API 사이 호출은 REST를 유지한다. React UI에는 Google 로그인, YouTube URL 메타데이터(개요·설명·챕터·히트맵), LLM·장르·STT·렌더링 설정, 분석 시작, 원본·렌더링 결과 로컬 미리보기, 챕터·후보 구간 선택과 AI 추천 복원, 자막 조회·수정·재렌더링이 이전되었다. 상세 대응표는 `UI_PARITY.md`를 참고한다. 전환이 끝난 정적 UI와 `/legacy` 경로는 제거했다.
# 현재 기준 안내

이 문서의 전환 순서는 완료된 분리 작업의 기록이다. 현재 실행, 도구, 서버 연동 범위는 [CURRENT_SCOPE.md](CURRENT_SCOPE.md)를 우선 기준으로 한다.

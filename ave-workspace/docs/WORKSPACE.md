# 워크스페이스 구성

## 목적

AVE는 모든 모듈을 하나의 루트 Git 저장소에서 관리한다.

루트 워크스페이스는 모듈을 한 디렉터리에서 함께 열고 개발하기 위한 작업 공간이다.

## 기본 구조

```text
AVE/
├─ docs/
├─ modules/
├─ AGENTS.md
└─ README.md
```

각 애플리케이션과 서비스는 `modules/` 아래에 위치한다.

```text
modules/
├─ ave-client/
├─ ave-dist/
├─ ave-server/
└─ ave-whisper-api/
```

## 저장소 관리

각 `modules/<모듈>` 디렉터리의 소스 코드와 문서는 루트 Git 저장소에서 추적한다.

모듈별 구현 사항은 해당 모듈 디렉터리에서 관리하고, 변경 사항은 루트 저장소에서 커밋한다.

여러 모듈에 공통으로 영향을 주는 내용은 루트 `docs/`에 기록한다.

## 워크스페이스 구성

새 개발 환경에서는 루트 저장소를 복제하면 모든 모듈이 `modules/` 아래에 함께 준비된다.

## 현재 기준 안내

프로젝트 전체 처리 흐름과 저장 원칙은 [SYSTEM_GUIDELINES.md](SYSTEM_GUIDELINES.md)를, 모듈의 현재 책임과 데이터 경계는 [CURRENT_ARCHITECTURE.md](CURRENT_ARCHITECTURE.md)를 따른다.

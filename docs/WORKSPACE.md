# 워크스페이스 구성

## 목적

AVE는 여러 모듈을 독립적인 Git 저장소로 관리한다.

루트 워크스페이스는 이러한 저장소를 한 디렉터리에서 함께 열고 개발하기 위한 작업 공간이다.

## 기본 구조

```text
AVE/
├─ docs/
├─ modules/
├─ AGENTS.md
├─ README.md
└─ setup.bat
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

각 `modules/<모듈>` 디렉터리는 독립적인 Git 저장소이다.

따라서 다음 사항을 구분한다.

* 루트 워크스페이스: 프로젝트 전체 문서와 개발 환경 구성
* 각 모듈 저장소: 해당 모듈의 실제 소스 코드와 모듈별 문서

모듈별 구현 사항은 가능한 한 해당 모듈 저장소에서 관리한다.

여러 모듈에 공통으로 영향을 주는 내용만 루트 `docs/`에 기록한다.

## 워크스페이스 구성

새 개발 환경에서는 루트의 `setup.bat`를 실행해 필요한 모듈 저장소를 `modules/` 아래로 내려받을 수 있다. 스크립트는 이미 존재하는 모듈 디렉터리를 건너뛴다.

## 현재 기준 안내

프로젝트 전체 처리 흐름과 저장 원칙은 [SYSTEM_GUIDELINES.md](SYSTEM_GUIDELINES.md)를, 모듈의 현재 책임과 데이터 경계는 [CURRENT_ARCHITECTURE.md](CURRENT_ARCHITECTURE.md)를 따른다. `setup.bat`는 저장소를 준비하는 용도이며, 클라이언트 프로그램의 실행 진입점은 아니다.

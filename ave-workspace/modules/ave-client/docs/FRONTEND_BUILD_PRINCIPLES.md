# 프런트엔드 빌드 원칙

## 단일 구현 위치

사용자에게 보이는 UI의 구조, 동작, 애니메이션, 스타일은 반드시 `ui/src/`의 React·TypeScript·CSS 원본에 구현한다.

`static/ui/`는 `npm run build`가 생성하는 배포용 산출물이다. 해시가 포함된 JavaScript·CSS 파일이나 `static/ui/index.html`을 직접 수정해 기능을 추가하지 않는다.

## 변경 및 확인 절차

1. `ui/src/`에서 UI를 수정한다.
2. `ui/`에서 `npm run build`를 실행한다.
3. 생성된 `static/ui/`를 FastAPI가 제공하는 화면에서 확인한다.

이 원칙에 따라 UI 기능은 빌드 후에도 동일하게 동작하며, 다음 빌드에서 사라지지 않는다.

## 예외

긴급한 화면 복구가 필요해도 `static/ui/` 직접 수정은 임시 진단 용도로만 사용한다. 같은 작업 안에서 반드시 `ui/src/` 원본 구현으로 옮기고 재빌드해 마무리한다.

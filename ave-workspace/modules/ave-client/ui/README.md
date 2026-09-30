# AVE 클라이언트 UI

이 디렉터리는 AVE 클라이언트의 React·TypeScript·Vite UI 소스입니다.

* 개발 서버: `npm run dev`
* 정적 빌드: `npm run build`
* 빌드 결과: `../static/ui/`

일반 실행은 UI 개발 서버가 아니라 저장소 루트에서 `python -m app`을 사용합니다. 로컬 FastAPI 서버가 빌드된 UI를 `/`와 `/ui/`에서 제공합니다.

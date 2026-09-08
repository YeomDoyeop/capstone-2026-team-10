# faster-whisper v1 아카이브

WhisperX 전환 전 AVE RunPod worker의 전사 엔진을 보존한 디렉터리입니다.

이 구현은 `faster-whisper==1.2.1`, `large-v3`, `float16`, `beam_size=5`, VAD를 사용하고 강제 정렬 없이 faster-whisper 세그먼트 시각을 그대로 반환했습니다. 신규 배포에는 사용하지 않습니다.

* `transcription.py`: 기존 전사 진입점
* `requirements.txt`: 기존 핵심 모델 의존성
* `Dockerfile`: 저장소 루트를 빌드 컨텍스트로 사용하는 레거시 worker 이미지 정의

복원 시 현재 `src/input_audio.py`, `src/request.py`, `src/timestamps.py` 계약과 함께 사용해야 합니다.
레거시 이미지를 다시 만들어야 할 때만 저장소 루트에서 다음 명령을 사용합니다.

```powershell
docker build --platform linux/amd64 `
  -f legacy/faster-whisper-v1/Dockerfile `
  -t <dockerhub-user>/ave-whisper-api:faster-whisper-v1 .
```

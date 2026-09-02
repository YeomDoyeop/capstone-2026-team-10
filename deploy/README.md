# Ubuntu VM 배포 안내

이 구성은 `ave-server.duckdns.org`에서 다음 두 기능을 함께 실행한다.

* `https://ave-server.duckdns.org/`: AVE 서버 API
* `https://ave-server.duckdns.org/files/<파일명>`: 원격 Whisper 전사용 임시 오디오 파일

원본 영상과 렌더링 결과 영상은 이 VM에 저장하지 않는다. `/files/`에는 전사 중인 오디오만 짧은 시간 보관하고 자동 삭제한다.

## 1. 사전 준비

다음 항목을 준비한다.

* Ubuntu VM의 SSH 관리자 계정: `ubuntu`
* VM 접속 주소: `ave-server.duckdns.org`
* VM에 연결된 DuckDNS 도메인과 TCP 80, 443 포트
* Windows에서 VM에 접속할 관리자 SSH 개인키
* Supabase와 RunPod 값을 담은 서버 전용 `.env`
* `docs/supabase_schema.sql`을 적용할 Supabase 프로젝트 관리자 권한

### Supabase 스키마 적용

배포 전에 Supabase Dashboard → **SQL Editor**에서 `modules/ave-server/docs/supabase_schema.sql`의 전체 내용을 실행한다. 이 단계가 없으면 서버는 실행되어도 분석 작업 이력을 기록할 수 없다.

### 네트워크 확인

Azure NSG 또는 VM 방화벽에서 다음 인바운드 규칙을 확인한다.

| 포트 | 용도 | 권장 원본 |
| --- | --- | --- |
| TCP 22 | 관리자 SSH | 신뢰할 수 있는 고정 IP만 |
| TCP 80 | Caddy의 HTTPS 인증서 발급과 HTTP 리디렉션 | Internet |
| TCP 443 | 서버 API와 Whisper의 임시 오디오 다운로드 | Internet |

DuckDNS가 VM 공인 IP를 가리키고 80, 443 포트가 열려 있어야 Caddy가 HTTPS 인증서를 자동 발급할 수 있다.

Azure VM에서 Docker 컨테이너가 `127.0.0.53` DNS 스텁을 사용하면 Let’s Encrypt 도메인을 조회하지 못할 수 있다. 배포 구성은 Caddy 컨테이너에 공개 DNS를 명시하므로, 이 경우에도 인증서 발급을 계속할 수 있다.

`ubuntu / ubuntu`처럼 비밀번호 로그인만 가능한 초기 VM은 자동 배포 전에 SSH 키 로그인을 준비해야 한다. PowerShell의 기본 `ssh`·`scp`는 비밀번호를 안전하게 자동 전달하지 않으므로, 개인키를 만드는 편이 반복 배포와 보안에 적합하다.

```powershell
# Windows에서 개인키 생성
ssh-keygen -t ed25519 -f "$env:USERPROFILE\.ssh\ave-server" -C "ave-server"

# 한 번만 비밀번호로 VM 접속 후 공개키 등록
ssh ubuntu@ave-server.duckdns.org
mkdir -p ~/.ssh
chmod 700 ~/.ssh
```

위 SSH 세션에서 로컬의 `C:\Users\<사용자>\.ssh\ave-server.pub` 내용을 `~/.ssh/authorized_keys`에 한 줄로 추가하고 `chmod 600 ~/.ssh/authorized_keys`를 실행한다. 이후 아래 배포 명령에 `ave-server` 개인키를 사용한다.

## 2. 로컬 환경 변수 파일 만들기

`modules/ave-server`에서 `.env` 파일을 준비한다. 이 파일은 Git에 커밋하지 않는다.

```powershell
Copy-Item .env.example .env
```

`.env`에는 Supabase와 RunPod 서버 키를 설정한다.

```env
SUPABASE_URL=https://<프로젝트-식별자>.supabase.co
SUPABASE_ANON_KEY=<공개-익명-키>
SUPABASE_SERVICE_ROLE_KEY=<서버-전용-키>
WHISPER_RUNPOD_ENDPOINT_ID=<RunPod-엔드포인트-ID>
RUNPOD_API_KEY=<RunPod-API-키>
AVE_DOMAIN=ave-server.duckdns.org
STT_FILES_DIR=/srv/ave-stt/files
```

## 3. 서버 배포

Docker가 설치되어 있으면 다음 명령을 실행한다. `<관리자-개인키>`만 실제 경로로 바꾼다.

```powershell
.\deploy\scripts\deploy.ps1 `
  -SshKeyPath "<관리자-개인키>" `
  -ServerEnvFilePath ".\.env"
```

Docker가 없는 VM이라면 처음 한 번만 `-InstallDocker`를 추가한다. 이 옵션은 Docker 공식 Ubuntu 패키지를 설치한다.

배포가 끝나면 브라우저 또는 PowerShell에서 상태를 확인한다.

```powershell
Invoke-RestMethod https://ave-server.duckdns.org/health
```

`{"status":"ok"}`가 반환되면 API가 실행 중이다.

## 4. 클라이언트 임시 오디오 설정

클라이언트의 `.env`에 서버 HTTPS 주소를 설정한다.

```env
AVE_SERVER_URL=https://ave-server.duckdns.org
```

클라이언트는 인증 헤더를 포함한 HTTP로 MP3를 서버에 올린다. 서버가 `ave-whisper-api` 호출을 마치면 임시 파일을 즉시 삭제한다. `/files/` URL은 RunPod worker가 해당 오디오를 내려받는 용도로만 사용한다.

## 5. 만료 파일 자동 삭제

VM에서 다음 명령으로 한 시간마다 24시간 지난 오디오를 삭제한다.

```bash
sudo install -m 755 /home/ubuntu/ave-server/deploy/scripts/cleanup-expired.sh /usr/local/bin/ave-stt-cleanup
sudo crontab -e
```

열린 편집기에 다음 줄을 추가한다.

```cron
15 * * * * /usr/local/bin/ave-stt-cleanup 24
```

## 재배포와 로그 확인

코드를 바꾼 뒤 같은 `deploy.ps1` 명령을 다시 실행하면 이미지를 다시 빌드하고 컨테이너를 교체한다.

VM에서 로그를 보려면 다음을 실행한다.

```bash
cd /home/ubuntu/ave-server/deploy
sudo docker compose --env-file ../.env logs -f
```

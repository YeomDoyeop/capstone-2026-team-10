[CmdletBinding()]
param(
    [string] $HostName = "ave-server.duckdns.org",
    [string] $AdminUser = "ubuntu",
    [Parameter(Mandatory)] [string] $SshKeyPath,
    [Parameter(Mandatory)] [string] $ServerEnvFilePath,
    [string] $RemoteDirectory = "/home/ubuntu/ave-server",
    [switch] $InstallDocker
)

$ErrorActionPreference = "Stop"

@($SshKeyPath, $ServerEnvFilePath) | ForEach-Object {
    if (!(Test-Path -LiteralPath $_ -PathType Leaf)) {
        throw "필수 파일을 찾을 수 없습니다: $_"
    }
}

$projectDirectory = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$remote = "${AdminUser}@${HostName}"

ssh -i $SshKeyPath $remote "mkdir -p '$RemoteDirectory/deploy/scripts'"
if ($LASTEXITCODE -ne 0) { throw "VM 배포 디렉터리를 만들지 못했습니다." }

if ($InstallDocker) {
    scp -i $SshKeyPath "$projectDirectory\deploy\scripts\install-docker-ubuntu.sh" "${remote}:$RemoteDirectory/deploy/scripts/"
    ssh -t -i $SshKeyPath $remote "sudo bash '$RemoteDirectory/deploy/scripts/install-docker-ubuntu.sh'"
    if ($LASTEXITCODE -ne 0) { throw "Docker 설치에 실패했습니다." }
}

scp -i $SshKeyPath "$projectDirectory\Dockerfile" "$projectDirectory\requirements.txt" "${remote}:$RemoteDirectory/"
if ($LASTEXITCODE -ne 0) { throw "서버 빌드 파일 복사에 실패했습니다." }
scp -i $SshKeyPath -r "$projectDirectory\app" "${remote}:$RemoteDirectory/"
if ($LASTEXITCODE -ne 0) { throw "서버 애플리케이션 복사에 실패했습니다." }
scp -i $SshKeyPath "$projectDirectory\deploy\compose.yaml" "$projectDirectory\deploy\Caddyfile" "${remote}:$RemoteDirectory/deploy/"
if ($LASTEXITCODE -ne 0) { throw "배포 구성 파일 복사에 실패했습니다." }
scp -i $SshKeyPath -r "$projectDirectory\deploy\scripts" "${remote}:$RemoteDirectory/deploy/"
if ($LASTEXITCODE -ne 0) { throw "배포 스크립트 복사에 실패했습니다." }
scp -i $SshKeyPath $ServerEnvFilePath "${remote}:$RemoteDirectory/.env"
if ($LASTEXITCODE -ne 0) { throw "서버 환경 변수 파일 복사에 실패했습니다." }

$command = "chmod 600 '$RemoteDirectory/.env' && sudo install -d -m 755 /srv/ave-stt/files && cd '$RemoteDirectory/deploy' && sudo docker compose --env-file ../.env up -d --build"
ssh -t -i $SshKeyPath $remote $command
if ($LASTEXITCODE -ne 0) { throw "서버 배포 또는 실행에 실패했습니다." }

Write-Host "배포가 완료되었습니다: https://$HostName/health"

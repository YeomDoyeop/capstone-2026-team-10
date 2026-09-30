param(
    [string]$BinDirectory = (Join-Path $PSScriptRoot "..\bin")
)

$ErrorActionPreference = "Stop"
$resolvedBin = [System.IO.Path]::GetFullPath($BinDirectory)
New-Item -ItemType Directory -Force -Path $resolvedBin | Out-Null

$ytDlpPath = Join-Path $resolvedBin "yt-dlp.exe"
Invoke-WebRequest -Uri "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe" -OutFile $ytDlpPath

$downloadId = [guid]::NewGuid().ToString("N")
$archivePath = Join-Path ([System.IO.Path]::GetTempPath()) ("ave-ffmpeg-" + $downloadId + ".zip")
$extractPath = Join-Path ([System.IO.Path]::GetTempPath()) ("ave-ffmpeg-" + $downloadId)
Invoke-WebRequest -Uri "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip" -OutFile $archivePath
Expand-Archive -Path $archivePath -DestinationPath $extractPath -Force

$ffmpegFile = Get-ChildItem -Path $extractPath -Filter "ffmpeg.exe" -Recurse | Select-Object -First 1
$ffprobeFile = Get-ChildItem -Path $extractPath -Filter "ffprobe.exe" -Recurse | Select-Object -First 1
if (-not $ffmpegFile -or -not $ffprobeFile) {
    throw "FFmpeg 압축 파일에서 ffmpeg.exe 또는 ffprobe.exe를 찾지 못했습니다."
}
Copy-Item -LiteralPath $ffmpegFile.FullName -Destination (Join-Path $resolvedBin "ffmpeg.exe") -Force
Copy-Item -LiteralPath $ffprobeFile.FullName -Destination (Join-Path $resolvedBin "ffprobe.exe") -Force

Write-Output "도구 배치 완료: $resolvedBin"

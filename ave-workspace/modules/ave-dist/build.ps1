[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9A-Za-z][0-9A-Za-z._-]*$')]
    [string]$Version,

    [string]$ClientDir = (Join-Path $PSScriptRoot '..\ave-client'),
    [string]$PythonCommand = 'py',
    [string]$EnvFile = '',
    [switch]$SkipUiBuild
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw 'AVE client packaging is supported only on Windows.'
}

$distRoot = (Resolve-Path $PSScriptRoot).Path
$clientRoot = (Resolve-Path $ClientDir).Path
$buildRoot = Join-Path $distRoot '.build'
$releaseRoot = Join-Path $distRoot 'release'
$packageName = "AVE-client-$Version-windows-x64"
$packageDir = Join-Path $releaseRoot $packageName
$archivePath = Join-Path $releaseRoot "$packageName.zip"
$venvRoot = Join-Path $buildRoot 'venv'
$venvPython = Join-Path $venvRoot 'Scripts\python.exe'
$uiStage = Join-Path $buildRoot 'ui'
$staticStage = Join-Path $buildRoot 'static\ui'
$iconFile = Join-Path $buildRoot 'ave.ico'
$updaterIconFile = Join-Path $buildRoot 'ave-updater.ico'
if (-not $EnvFile) { $EnvFile = Join-Path $clientRoot '.env' }
$resolvedEnvFile = (Resolve-Path $EnvFile).Path
$bakedEnvFile = Join-Path $buildRoot '.env'

$requiredClientFiles = @(
    'app\__main__.py',
    'app\main.py',
    'app\desktop.py',
    'requirements.txt'
)
foreach ($relativePath in $requiredClientFiles) {
    $candidate = Join-Path $clientRoot $relativePath
    if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
        throw "Required client file not found: $candidate"
    }
}

if (Test-Path -LiteralPath $buildRoot) {
    Remove-Item -LiteralPath $buildRoot -Recurse -Force
}
New-Item -ItemType Directory -Path $buildRoot | Out-Null
New-Item -ItemType Directory -Path $releaseRoot -Force | Out-Null
Copy-Item -LiteralPath $resolvedEnvFile -Destination $bakedEnvFile

if ($SkipUiBuild) {
    $sourceStatic = Join-Path $clientRoot 'static\ui'
    if (-not (Test-Path -LiteralPath (Join-Path $sourceStatic 'index.html'))) {
        throw '-SkipUiBuild requires ave-client/static/ui/index.html.'
    }
    New-Item -ItemType Directory -Path (Split-Path $staticStage) -Force | Out-Null
    Copy-Item -LiteralPath $sourceStatic -Destination $staticStage -Recurse
}
else {
    $sourceUi = Join-Path $clientRoot 'ui'
    Copy-Item -LiteralPath $sourceUi -Destination $uiStage -Recurse
    Push-Location $uiStage
    try {
        & npm.cmd ci
        if ($LASTEXITCODE -ne 0) { throw "npm ci failed: $LASTEXITCODE" }
        & npm.cmd run build
        if ($LASTEXITCODE -ne 0) { throw "UI build failed: $LASTEXITCODE" }
    }
    finally {
        Pop-Location
    }
}

if (-not (Test-Path -LiteralPath (Join-Path $staticStage 'index.html'))) {
    throw "UI output not found: $staticStage"
}

if ($PythonCommand -eq 'py') {
    & py -3 -m venv $venvRoot
}
else {
    & $PythonCommand -m venv $venvRoot
}
if ($LASTEXITCODE -ne 0) { throw "Python virtual environment creation failed: $LASTEXITCODE" }

$pythonInfo = (& $venvPython -c "import platform, struct; print(f'{platform.python_version()}|{struct.calcsize(chr(80)) * 8}')").Trim()
$pythonParts = $pythonInfo.Split('|')
if ([version]$pythonParts[0] -lt [version]'3.11' -or $pythonParts[1] -ne '64') {
    throw "Python 3.11+ x64 is required. Current runtime: $pythonInfo"
}

& $venvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed: $LASTEXITCODE" }
& $venvPython -m pip install -r (Join-Path $distRoot 'requirements-build.txt')
if ($LASTEXITCODE -ne 0) { throw "Build dependency installation failed: $LASTEXITCODE" }
& $venvPython -m pip install -r (Join-Path $clientRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw "Client dependency installation failed: $LASTEXITCODE" }

& $venvPython (Join-Path $distRoot 'create_icon.py') $iconFile
if ($LASTEXITCODE -ne 0) { throw "Application icon generation failed: $LASTEXITCODE" }
& $venvPython (Join-Path $distRoot 'create_updater_icon.py') $updaterIconFile
if ($LASTEXITCODE -ne 0) { throw "Updater icon generation failed: $LASTEXITCODE" }

$env:AVE_CLIENT_DIR = $clientRoot
$env:AVE_ENV_FILE = $bakedEnvFile
$env:AVE_ICON_FILE = $iconFile
$env:AVE_UPDATER_ICON_FILE = $updaterIconFile
try {
    & $venvPython -m PyInstaller `
        --noconfirm `
        --clean `
        --distpath (Join-Path $buildRoot 'dist') `
        --workpath (Join-Path $buildRoot 'pyinstaller') `
        (Join-Path $distRoot 'ave-client.spec')
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed: $LASTEXITCODE" }
    & $venvPython -m PyInstaller `
        --noconfirm `
        --clean `
        --distpath (Join-Path $buildRoot 'dist') `
        --workpath (Join-Path $buildRoot 'pyinstaller-updater') `
        (Join-Path $distRoot 'ave-updater.spec')
    if ($LASTEXITCODE -ne 0) { throw "Updater build failed: $LASTEXITCODE" }
}
finally {
    Remove-Item Env:\AVE_CLIENT_DIR -ErrorAction SilentlyContinue
    Remove-Item Env:\AVE_ENV_FILE -ErrorAction SilentlyContinue
    Remove-Item Env:\AVE_ICON_FILE -ErrorAction SilentlyContinue
    Remove-Item Env:\AVE_UPDATER_ICON_FILE -ErrorAction SilentlyContinue
}

if (Test-Path -LiteralPath $packageDir) {
    Remove-Item -LiteralPath $packageDir -Recurse -Force
}
New-Item -ItemType Directory -Path $packageDir | Out-Null
Move-Item -LiteralPath (Join-Path $buildRoot 'dist\ave_client.exe') -Destination $packageDir
Move-Item -LiteralPath (Join-Path $buildRoot 'dist\ave_updater.exe') -Destination $packageDir
Copy-Item -LiteralPath (Join-Path $distRoot 'readme.txt') -Destination $packageDir
New-Item -ItemType Directory -Path (Join-Path $packageDir 'static') | Out-Null
Copy-Item -LiteralPath $staticStage -Destination (Join-Path $packageDir 'static\ui') -Recurse
Copy-Item -LiteralPath (Join-Path $clientRoot 'prompts') -Destination (Join-Path $packageDir 'prompts') -Recurse

$requiredPackageFiles = @(
    'ave_client.exe',
    'ave_updater.exe',
    'readme.txt',
    'static\ui\index.html'
    'prompts\user\ai_news.json'
    'prompts\system\chapter.json'
    'prompts\schemas\chapter.json'
    'prompts\schemas\whisper_settings.json'
)
foreach ($relativePath in $requiredPackageFiles) {
    $candidate = Join-Path $packageDir $relativePath
    if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
        throw "Required package file not found: $candidate"
    }
}

if (Test-Path -LiteralPath $archivePath) {
    Remove-Item -LiteralPath $archivePath -Force
}
Compress-Archive -LiteralPath $packageDir -DestinationPath $archivePath -CompressionLevel Optimal

Write-Host "Build completed: $archivePath"

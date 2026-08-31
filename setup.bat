@echo off
chcp 65001 >nul
setlocal

title AVE Workspace Setup

set ROOT=%~dp0
set MODULES_DIR=%ROOT%modules

echo ========================================
echo AVE Workspace Setup
echo ========================================
echo.

where git >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Git이 설치되어 있지 않거나 PATH에 등록되어 있지 않습니다.
    echo.
    goto :end
)

if not exist "%MODULES_DIR%" (
    echo [CREATE] modules 디렉토리를 생성합니다.
    mkdir "%MODULES_DIR%"
)

call :clone_repo ave-client https://github.com/AMGN-Capstone/ave-client.git
call :clone_repo ave-dist https://github.com/AMGN-Capstone/ave-dist.git
call :clone_repo ave-server https://github.com/AMGN-Capstone/ave-server.git
call :clone_repo ave-whisper-api https://github.com/AMGN-Capstone/ave-whisper-api.git
goto :end


:clone_repo
set REPO_NAME=%~1
set REPO_URL=%~2
set DEST=%MODULES_DIR%\%REPO_NAME%

if exist "%DEST%\.git" (
    echo [SKIP] %REPO_NAME% 저장소가 이미 존재합니다.
    exit /b 0
)

if exist "%DEST%" (
    echo [SKIP] %DEST% 디렉토리가 이미 존재합니다.
    exit /b 0
)

echo [CLONE] %REPO_NAME%
git clone "%REPO_URL%" "%DEST%"

exit /b 0


:end

echo.
echo ========================================
echo AVE Workspace Setup has finished
echo ========================================
echo.

pause
endlocal

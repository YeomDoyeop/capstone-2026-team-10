@echo off
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
    echo [ERROR] Git is not installed or is not available in PATH.
    echo.
    goto :end
)

if not exist "%MODULES_DIR%" (
    echo [CREATE] Creating the modules directory.
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
    echo [SKIP] The %REPO_NAME% repository already exists.
    exit /b 0
)

if exist "%DEST%" (
    echo [SKIP] The %DEST% directory already exists.
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

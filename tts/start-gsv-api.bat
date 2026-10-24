@echo off
setlocal
title GPT-SoVITS API - 9883 - DO NOT CLOSE
cd /d "%~dp0"

rem Look for a GPT-SoVITS folder next to this script or its parent.
set "GSV_DIR="
for /d %%D in ("%~dp0GPT-SoVITS*") do if exist "%%~fD\api_v2.py" set "GSV_DIR=%%~fD"
if not defined GSV_DIR for /d %%D in ("%~dp0..\GPT-SoVITS*") do if exist "%%~fD\api_v2.py" set "GSV_DIR=%%~fD"

if not defined GSV_DIR (
    echo [ERROR] GPT-SoVITS not found.
    echo.
    echo Put the GPT-SoVITS folder here:
    echo   %~dp0GPT-SoVITS
    echo Then run this script again.
    pause
    exit /b 1
)

set "GSV_PYTHON=%GSV_DIR%\runtime\python.exe"
if not exist "%GSV_PYTHON%" set "GSV_PYTHON=python"

cd /d "%GSV_DIR%"
set "PATH=%GSV_DIR%\runtime;%PATH%"
"%GSV_PYTHON%" api_v2.py -a 127.0.0.1 -p 9883 -c GPT_SoVITS\configs\tts_infer.yaml > "%~dp0api_v2.log" 2>&1
echo.
echo [STOPPED] GPT-SoVITS API has exited. See api_v2.log
pause

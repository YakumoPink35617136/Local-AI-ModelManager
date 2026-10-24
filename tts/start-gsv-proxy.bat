@echo off
setlocal
title GPT-SoVITS Proxy - 9882 - DO NOT CLOSE
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
set "VOICE_LIBRARY=%~dp0VoiceLibrary"

if not exist "%~dp0gsv_proxy.py" (
    echo [ERROR] gsv_proxy.py not found next to this bat.
    pause
    exit /b 1
)

if not defined TTS_PYTHON if exist "%~dp0.venv\Scripts\python.exe" set "TTS_PYTHON=%~dp0.venv\Scripts\python.exe"
if not defined TTS_PYTHON if exist "%~dp0python_embed\python.exe" set "TTS_PYTHON=%~dp0python_embed\python.exe"

if defined TTS_PYTHON goto run_custom

py -3 -c "import sys" >nul 2>nul
if not errorlevel 1 goto run_py

python -c "import sys" >nul 2>nul
if not errorlevel 1 goto run_python

echo [ERROR] Python not found.
echo         Install Python 3.10+ or set TTS_PYTHON to python.exe.
pause
exit /b 1

:run_custom
"%TTS_PYTHON%" "%~dp0gsv_proxy.py"
goto done

:run_python
python "%~dp0gsv_proxy.py"
goto done

:run_py
py -3 "%~dp0gsv_proxy.py"
goto done

:done
echo.
echo [STOPPED] GPT-SoVITS proxy has exited.
pause

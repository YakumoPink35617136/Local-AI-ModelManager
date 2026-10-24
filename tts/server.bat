@echo off
setlocal
title Genie TTS - 9880 - DO NOT CLOSE
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"

if not exist "%~dp0server.py" (
    echo [ERROR] server.py not found next to this bat.
    pause
    exit /b 1
)

if not defined GENIE_PYTHON if exist "%~dp0.venv\Scripts\python.exe" set "GENIE_PYTHON=%~dp0.venv\Scripts\python.exe"
if not defined GENIE_PYTHON if exist "%~dp0python_embed\python.exe" set "GENIE_PYTHON=%~dp0python_embed\python.exe"

if defined GENIE_PYTHON goto run_custom

py -3 -c "import sys" >nul 2>nul
if not errorlevel 1 goto run_py

python -c "import sys" >nul 2>nul
if not errorlevel 1 goto run_python

echo [ERROR] Python not found.
echo         Install Python 3.10+ or set GENIE_PYTHON to python.exe.
pause
exit /b 1

:run_custom
"%GENIE_PYTHON%" "%~dp0server.py"
goto done

:run_python
python "%~dp0server.py"
goto done

:run_py
py -3 "%~dp0server.py"
goto done

:done
echo.
echo [STOPPED] Genie TTS server has exited.
pause

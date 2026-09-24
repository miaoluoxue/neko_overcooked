@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set NEKO_INPUT=virtual
if not defined NEKO_PYTHON set "NEKO_PYTHON=python"
set "PY=%NEKO_PYTHON%"
"%PY%" --version >nul 2>&1
if errorlevel 1 (
  echo Python runtime not found.
  pause
  exit /b 1
)
start "" "C:\Program Files (x86)\Steam\steam.exe" -applaunch 728880
echo Enter the game main menu. AI joins as Player Two automatically.
echo Press Ctrl+C here to stop safely. Do not close this window while AI is playing.
"%PY%" -u run_watch.py
pause

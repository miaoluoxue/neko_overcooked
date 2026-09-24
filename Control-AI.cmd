@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
if not defined NEKO_PYTHON set "NEKO_PYTHON=python"
set "PY=%NEKO_PYTHON%"
if "%~1"=="" (
  "%PY%" tools\ctl.py status --json
) else (
  "%PY%" tools\ctl.py %*
)
pause

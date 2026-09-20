@echo off
setlocal
if not defined NEKO_PYTHON set "NEKO_PYTHON=python"
cd /d "%~dp0"
set PYTHONUTF8=1
start "" "C:\Program Files (x86)\Steam\steam.exe" -applaunch 728880
"%NEKO_PYTHON%" -X utf8 -u run_campaign.py
pause

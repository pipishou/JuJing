@echo off
chcp 65001 >nul
setlocal
set "ROOT=%~dp0.."
cd /d "%ROOT%"
set "PY=%ROOT%\..\..\venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" -B tools\flywheel_tick.py --due >> "%ROOT%\runtime\logs\flywheel-due.log" 2>&1
exit /b %ERRORLEVEL%

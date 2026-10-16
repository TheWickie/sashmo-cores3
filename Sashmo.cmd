@echo off
setlocal
cd /d "%~dp0"
where pythonw.exe >nul 2>nul
if %errorlevel%==0 (
  start "" pythonw.exe "%~dp0desktop\sashmo_desktop.py"
) else (
  start "" python.exe "%~dp0desktop\sashmo_desktop.py"
)

@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  python -m venv ".venv"
  if errorlevel 1 goto :failed
)
".venv\Scripts\python.exe" -m pip install -q -r "deck\requirements.txt"
if errorlevel 1 goto :failed
python "desktop\service_host.py" start >nul 2>nul
start "" ".venv\Scripts\pythonw.exe" -m deck --root "%~dp0" --hardware
exit /b 0
:failed
echo Sashmo Deck dependencies could not be installed. Check Python and your connection.
pause
exit /b 1

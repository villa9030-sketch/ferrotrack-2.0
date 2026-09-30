@echo off
REM FerroTrack: avvio SENZA finestra (server waitress, log in logs\ferrotrack.log).
REM Se e' gia' acceso non parte una seconda volta.
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  echo Manca .venv: vedi INSTALLAZIONE.md
  pause
  exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" run.py
exit /b 0

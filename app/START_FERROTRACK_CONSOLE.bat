@echo off
REM FerroTrack con la finestra dei messaggi (per controllare cosa succede).
REM Chiudere la finestra o CTRL+C ferma il server.
cd /d "%~dp0"
title FerroTrack
".venv\Scripts\python.exe" run.py
pause

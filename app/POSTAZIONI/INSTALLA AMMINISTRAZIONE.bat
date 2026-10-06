@echo off
REM Icona FerroTrack Amministrazione sul desktop (senza apertura automatica)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0installa_postazione.ps1" -Stazione amministrazione
pause

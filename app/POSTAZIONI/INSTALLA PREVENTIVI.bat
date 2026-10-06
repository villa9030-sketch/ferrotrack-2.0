@echo off
REM Icona FerroTrack Preventivi (commerciale) sul desktop, si apre da sola all'accensione
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0installa_postazione.ps1" -Stazione commerciale -AllAvvio
pause

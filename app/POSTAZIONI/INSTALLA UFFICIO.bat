@echo off
REM Icona FerroTrack Ufficio sul desktop, si apre da sola all'accensione
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0installa_postazione.ps1" -Stazione ufficio -AllAvvio
pause

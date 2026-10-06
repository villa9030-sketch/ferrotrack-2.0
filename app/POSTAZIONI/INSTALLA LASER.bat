@echo off
REM Icona FerroTrack Laser a schermo intero, si apre da sola all'accensione
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0installa_postazione.ps1" -Stazione laser -AllAvvio -SchermoIntero
pause

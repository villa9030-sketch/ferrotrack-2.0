@echo off
rem Prova notturna di FerroTrack su COPIA (ferrotrack-stress), senza finestre,
rem a priorita' bassa. Log e report in banco_motore\stress\<data_ora>\.
rem Arresto forzato alle 07:30 comunque vada. Lantek: solo stub.
rem Argomenti in piu' sostituiscono quelli di default, es. per la prova breve:
rem   avvia_notte.bat --profilo rapido --durata-ore 0.25
setlocal
set "QUI=%~dp0"
set "RADICE=%QUI%..\..\..\.."
set "PYW=%RADICE%\ferrotrack-2.0\app\.venv\Scripts\pythonw.exe"
set "PYTHONIOENCODING=utf-8"
for /f %%t in ('powershell -NoProfile -NonInteractive -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set "STAMP=%%t"
set "RUN=%RADICE%\banco_motore\stress\%STAMP%"
if not exist "%RUN%" mkdir "%RUN%"
cd /d "%QUI%"
rem Prima si unisce il codice di produzione (commit di ferrotrack-2.0) in prove/stress:
rem esito in %RUN%\allineamento.json. Per saltarlo: set FT_STRESS_SENZA_MERGE=1
if not "%FT_STRESS_SENZA_MERGE%"=="1" "%RADICE%\ferrotrack-2.0\app\.venv\Scripts\python.exe" "%QUI%allinea_produzione.py" --cartella "%RUN%" >"%RUN%\allineamento.log" 2>&1
start "FerroTrack prova notturna" /MIN /BELOWNORMAL "%PYW%" "%QUI%esegui_notte.py" --profilo notte --durata-ore 13 --cartella "%RUN%" %* >"%RUN%\console.log" 2>&1
endlocal
exit /b 0

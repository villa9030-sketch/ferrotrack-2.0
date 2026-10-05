@echo off
REM FerroTrack Laser sul SECONDO monitor, a schermo intero, senza barre del browser.
REM Il secondo monitor si presume a destra del principale (1920 px). Se e' a
REM sinistra cambia POS in -1920,0.
set URL=http://192.168.1.10:5000/laser.html
set POS=1920,0
set EDGE=%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe
if not exist "%EDGE%" set EDGE=%ProgramFiles%\Microsoft\Edge\Application\msedge.exe
start "" "%EDGE%" --new-window --app=%URL% --window-position=%POS% --start-fullscreen --user-data-dir="%LOCALAPPDATA%\FerroTrackLaser"

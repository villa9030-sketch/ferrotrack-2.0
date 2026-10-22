' Lancia controllo_server.ps1 senza nessuna finestra (attivita' "FerroTrack controllo").
Set sh = CreateObject("WScript.Shell")
qui = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
sh.Run "powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File """ & qui & "\controllo_server.ps1""", 0, False

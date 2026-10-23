' Lancia quadratura_giornaliera.py senza nessuna finestra (attivita' "FerroTrack quadratura", ogni mattina alle 06:45).
Set sh = CreateObject("WScript.Shell")
qui = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
app = CreateObject("Scripting.FileSystemObject").GetParentFolderName(qui)
sh.CurrentDirectory = app
sh.Run """" & app & "\.venv\Scripts\pythonw.exe"" """ & qui & "\quadratura_giornaliera.py""", 0, False

<#
FerroTrack: avvio automatico e comandi del server.

  powershell -ExecutionPolicy Bypass -File tools\installa_avvio.ps1              installa l'avvio all'accesso a Windows
  powershell -ExecutionPolicy Bypass -File tools\installa_avvio.ps1 -Accensione  all'accensione del PC, anche senza accesso (serve amministratore)
  powershell -ExecutionPolicy Bypass -File tools\installa_avvio.ps1 -Rimuovi     toglie l'avvio automatico
  powershell -ExecutionPolicy Bypass -File tools\installa_avvio.ps1 -Ferma       spegne il server (es. prima di un ripristino)
  powershell -ExecutionPolicy Bypass -File tools\installa_avvio.ps1 -Avvia       accende il server adesso
  powershell -ExecutionPolicy Bypass -File tools\installa_avvio.ps1 -Stato       dice se e' acceso

Il server parte senza finestra (pythonw run.py); i messaggi sono in logs\ferrotrack.log.
Se si ferma per un errore, Windows lo riavvia (3 tentativi, uno al minuto).
#>
param(
  [switch]$Accensione,
  [switch]$Rimuovi,
  [switch]$Ferma,
  [switch]$Avvia,
  [switch]$Stato
)

$ErrorActionPreference = 'Stop'
$App = Split-Path -Parent $PSScriptRoot
$Pyw = Join-Path $App '.venv\Scripts\pythonw.exe'
$Nome = 'FerroTrack server'

function Processi-Server {
  Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -and $_.CommandLine -like '*run.py*' -and
    ($_.CommandLine -like "*$App*" -or ($_.ExecutablePath -and $_.ExecutablePath -like "$App*"))
  }
}

function Acceso {
  try { $c = New-Object Net.Sockets.TcpClient; $c.Connect('127.0.0.1', 5000); $c.Close(); return $true } catch { return $false }
}

if ($Stato) {
  if (Acceso) { 'FerroTrack e'' acceso (porta 5000).' } else { 'FerroTrack e'' spento.' }
  $t = Get-ScheduledTask -TaskName $Nome -ErrorAction SilentlyContinue
  if ($t) { "Avvio automatico: installato ($($t.State))." } else { 'Avvio automatico: non installato.' }
  return
}

if ($Ferma) {
  $p = @(Processi-Server)
  if (-not $p.Count) { 'Nessun processo FerroTrack trovato.'; return }
  $p | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -Confirm:$false }
  Start-Sleep 2
  if (Acceso) { 'ATTENZIONE: la porta 5000 risponde ancora.' } else { "Server spento ($($p.Count) processi)." }
  return
}

if ($Avvia) {
  if (Acceso) { 'FerroTrack e'' gia'' acceso.'; return }
  Start-Process -FilePath $Pyw -ArgumentList 'run.py' -WorkingDirectory $App -WindowStyle Hidden
  Start-Sleep 8
  if (Acceso) { 'FerroTrack acceso: http://localhost:5000' } else { 'Non risponde ancora: guarda logs\ferrotrack.log' }
  return
}

if ($Rimuovi) {
  Unregister-ScheduledTask -TaskName $Nome -Confirm:$false -ErrorAction SilentlyContinue
  'Avvio automatico tolto.'
  return
}

if (-not (Test-Path $Pyw)) { throw "Manca $Pyw : crea prima il .venv (INSTALLAZIONE.md)." }
$azione = New-ScheduledTaskAction -Execute $Pyw -Argument 'run.py' -WorkingDirectory $App
$imp = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
  -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable
if ($Accensione) {
  $trig = New-ScheduledTaskTrigger -AtStartup
  $chi = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
  Register-ScheduledTask -TaskName $Nome -Action $azione -Trigger $trig -Settings $imp -Principal $chi -Force | Out-Null
  "Installato: FerroTrack parte all'accensione del PC (account di sistema)."
} else {
  $trig = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
  Register-ScheduledTask -TaskName $Nome -Action $azione -Trigger $trig -Settings $imp -Force | Out-Null
  "Installato: FerroTrack parte quando $env:USERNAME accede a Windows."
}

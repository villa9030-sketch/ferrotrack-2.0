<#
FerroTrack: avvio automatico e comandi del server.

  powershell -ExecutionPolicy Bypass -File tools\installa_avvio.ps1              installa l'avvio all'accesso a Windows
  powershell -ExecutionPolicy Bypass -File tools\installa_avvio.ps1 -Accensione  all'accensione del PC col tuo utente, anche senza accesso (amministratore + password di Windows)
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
  # Parte all'accensione COL TUO UTENTE, anche se nessuno accede a Windows.
  # Non piu' come SYSTEM: l'account di sistema non puo' scrivere nelle cartelle
  # condivise degli altri PC, e la copia di sicurezza esterna falliva sempre.
  # Windows chiede la password dell'utente una volta sola (non il PIN).
  $utente = "$env:USERDOMAIN\$env:USERNAME"
  $pw = Read-Host -AsSecureString "Password di Windows di $utente (non il PIN)"
  $chiaro = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($pw))
  $trig = New-ScheduledTaskTrigger -AtStartup
  Register-ScheduledTask -TaskName $Nome -Action $azione -Trigger $trig -Settings $imp `
    -User $utente -Password $chiaro -RunLevel Highest -Force | Out-Null
  $chiaro = $null
  "Installato: FerroTrack parte all'accensione del PC, con l'utente $utente."
} else {
  $trig = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
  Register-ScheduledTask -TaskName $Nome -Action $azione -Trigger $trig -Settings $imp -Force | Out-Null
  "Installato: FerroTrack parte quando $env:USERNAME accede a Windows."
}

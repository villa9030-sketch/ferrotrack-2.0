<#
Quadratura degli ordini ogni mattina (tools\quadratura_giornaliera.py).

  powershell -ExecutionPolicy Bypass -File tools\installa_quadratura.ps1           registra l'attivita' "FerroTrack quadratura" (ogni giorno alle 06:45)
  powershell -ExecutionPolicy Bypass -File tools\installa_quadratura.ps1 -Rimuovi  la toglie
  powershell -ExecutionPolicy Bypass -File tools\installa_quadratura.ps1 -Prova    la fa partire adesso (poi guarda logs\quadratura.log)

Gira col tuo utente, senza finestre (wscript + quadratura_giornaliera.vbs + pythonw),
come "FerroTrack controllo". Legge Lantek SOLO in lettura; non avvia nessun programma di Lantek.
Scrive logs\quadratura.log e, se qualche ordine non quadra, UNA notifica al laser e all'amministrazione.
#>
param(
  [switch]$Rimuovi,
  [switch]$Prova,
  [string]$Ora = '06:45'
)

$ErrorActionPreference = 'Stop'
$App = Split-Path -Parent $PSScriptRoot
$Vbs = Join-Path $PSScriptRoot 'quadratura_giornaliera.vbs'
$Nome = 'FerroTrack quadratura'

if ($Rimuovi) {
  Unregister-ScheduledTask -TaskName $Nome -Confirm:$false -ErrorAction SilentlyContinue
  "Attivita' '$Nome' tolta."
  return
}
if ($Prova) {
  Start-ScheduledTask -TaskName $Nome
  "Partita: tra un minuto guarda $App\logs\quadratura.log"
  return
}

$azione = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument "`"$Vbs`"" -WorkingDirectory $App
$quando = New-ScheduledTaskTrigger -Daily -At $Ora
$impost = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
  -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -MultipleInstances IgnoreNew
$chi = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $Nome -Action $azione -Trigger $quando -Settings $impost -Principal $chi `
  -Description 'FerroTrack: quadratura degli ordini aperti (cliente / FerroTrack / Lantek), una notifica se qualcosa non torna.' -Force | Out-Null
"Attivita' '$Nome' registrata: ogni giorno alle $Ora (senza finestre)."

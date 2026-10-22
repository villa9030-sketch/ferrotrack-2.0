<#
Controllo del server FerroTrack (attivita' pianificata "FerroTrack controllo",
ogni 5 minuti, senza finestre: la lancia controllo_server.vbs).

Il 22/10/2026 il server e' stato chiuso da Windows alle 06:30 senza errori e
nessuno se n'e' accorto fino a meta' mattina. Qui: se /api/health non
risponde per due volte di fila (30 s di distanza), si riaccende con
l'attivita' "FerroTrack server" e si scrive una riga in logs\controllo.log.
Non tocca nient'altro: niente database, niente file del programma.
#>
$ErrorActionPreference = 'SilentlyContinue'
$App = Split-Path -Parent $PSScriptRoot
$Log = Join-Path $App 'logs\controllo.log'

function Risponde {
  try {
    $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 10 'http://127.0.0.1:5000/api/health'
    return $r.StatusCode -eq 200
  } catch { return $false }
}

if (Risponde) { return }
Start-Sleep -Seconds 30
if (Risponde) { return }

$ora = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
Add-Content -Path $Log -Value "[$ora] il server non risponde: lo riaccendo" -Encoding utf8
Start-ScheduledTask -TaskName 'FerroTrack server'
Start-Sleep -Seconds 20
$esito = if (Risponde) { 'riacceso' } else { 'ANCORA SPENTO' }
$ora = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
Add-Content -Path $Log -Value "[$ora] $esito" -Encoding utf8

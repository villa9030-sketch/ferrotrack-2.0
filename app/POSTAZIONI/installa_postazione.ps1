<#
FerroTrack: collegamento sul desktop di una postazione PC (Ufficio, Commerciale, Laser, Amministrazione).

Si lancia dai file "INSTALLA ....bat" di questa cartella, SULLA POSTAZIONE (non sul server).
Crea sul desktop l'icona "FerroTrack <postazione>" che apre la pagina giusta in una
finestra senza barre del browser (Edge in modalita' app). Con -AllAvvio la apre anche
da sola quando si accende il PC.

Ogni postazione usa un profilo di Edge tutto suo: la registrazione del dispositivo
(fatta una volta col PIN dell'amministratore) resta salvata li' e non si mescola
con il browser normale.
#>
param(
  [Parameter(Mandatory = $true)]
  [ValidateSet('ufficio', 'commerciale', 'laser', 'amministrazione')]
  [string]$Stazione,
  [switch]$AllAvvio,
  [switch]$SchermoIntero,
  [string]$Server = 'http://192.168.1.10:5000'
)

$ErrorActionPreference = 'Stop'
$pagine = @{
  ufficio         = @('impiegata.html', 'FerroTrack Ufficio')
  commerciale     = @('preventivi.html', 'FerroTrack Preventivi')
  laser           = @('laser.html', 'FerroTrack Laser')
  amministrazione = @('admin.html', 'FerroTrack Amministrazione')
}
$pagina, $nome = $pagine[$Stazione]
$url = "$Server/$pagina"

$edge = @("${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
          "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $edge) { throw 'Microsoft Edge non trovato su questo PC.' }

# Il server risponde? Se no, il collegamento si crea lo stesso ma si avvisa.
$risponde = $true
try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 4 "$Server/api/health" | Out-Null } catch { $risponde = $false }

$profilo = Join-Path $env:LOCALAPPDATA ("FerroTrack_" + $Stazione)

# L'icona LS si copia sul PC: la cartella POSTAZIONI puo' stare su una chiavetta
$icona = "$edge,0"
$icoSrc = Join-Path $PSScriptRoot 'ferrotrack.ico'
if (Test-Path $icoSrc) {
  $icoDir = Join-Path $env:LOCALAPPDATA 'FerroTrack'
  New-Item -ItemType Directory -Force $icoDir | Out-Null
  Copy-Item $icoSrc (Join-Path $icoDir 'ferrotrack.ico') -Force
  $icona = Join-Path $icoDir 'ferrotrack.ico'
}
$modo = if ($SchermoIntero) { '--start-fullscreen' } else { '--start-maximized' }
$argomenti = "--app=$url $modo --user-data-dir=`"$profilo`" --no-first-run --disable-features=Translate"

function Crea-Collegamento($cartella) {
  $sh = New-Object -ComObject WScript.Shell
  $lnk = $sh.CreateShortcut((Join-Path $cartella "$nome.lnk"))
  $lnk.TargetPath = $edge
  $lnk.Arguments = $argomenti
  $lnk.WorkingDirectory = Split-Path $edge
  $lnk.IconLocation = $icona
  $lnk.Description = "FerroTrack - $Stazione"
  $lnk.Save()
}

Crea-Collegamento ([Environment]::GetFolderPath('Desktop'))
"Creato sul desktop: $nome  ->  $url"
if ($AllAvvio) {
  Crea-Collegamento ([Environment]::GetFolderPath('Startup'))
  "Si aprira' da solo all'accensione del PC."
}
if (-not $risponde) {
  ''
  "ATTENZIONE: il server $Server adesso non risponde."
  'Controlla che il PC server sia acceso e che questa postazione sia sulla stessa rete.'
}
''
'Prima volta: apri l''icona e registra la postazione col PIN dell''amministratore.'

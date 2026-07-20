<#
    Wohnungs-Radar starten (Windows/PowerShell).

    Startet das Backend in WSL2, wartet auf /health und öffnet Chrome im
    App-Modus. Gedacht als Ziel für den Windows Task Scheduler (Auto-Start
    bei Anmeldung) — Details in der README.

    Aufruf:   powershell -ExecutionPolicy Bypass -File .\start.ps1
              powershell -ExecutionPolicy Bypass -File .\start.ps1 -NoBrowser
#>

param(
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"

# Projektpfad in WSL2 (dieses Skript liegt im selben Verzeichnis).
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

# Port aus config.yaml lesen (Fallback 8765).
$port = 8765
$portLine = Select-String -Path (Join-Path $scriptDir "config.yaml") -Pattern '^\s+port:\s*(\d+)' | Select-Object -First 1
if ($portLine) { $port = [int]$portLine.Matches[0].Groups[1].Value }
$url = "http://localhost:$port"

# Backend in WSL2 starten. bash --login stellt sicher, dass conda/pyenv-Pfade
# geladen sind; start.sh --no-browser kümmert sich um venv und uvicorn.
Write-Host "→ Starte Backend in WSL2 …"
$wslCommand = "cd `"`$(wslpath '$scriptDir')`" && ./start.sh --no-browser"
$backend = Start-Process -FilePath "wsl.exe" `
    -ArgumentList "bash", "-lic", "`"$wslCommand`"" `
    -PassThru -WindowStyle Minimized

# Auf Erreichbarkeit warten.
Write-Host "→ Warte auf $url/health …"
$ready = $false
for ($i = 0; $i -lt 40; $i++) {
    try {
        $response = Invoke-WebRequest -Uri "$url/health" -UseBasicParsing -TimeoutSec 2
        if ($response.StatusCode -eq 200) { $ready = $true; break }
    } catch {
        Start-Sleep -Milliseconds 500
    }
}

if (-not $ready) {
    Write-Host "✗ Backend antwortet nicht. Prüfe logs/wohnungsradar.log in WSL2." -ForegroundColor Red
    exit 1
}
Write-Host "→ Backend bereit."

# Chrome im App-Modus öffnen.
if (-not $NoBrowser) {
    $chrome = "C:\Program Files\Google\Chrome\Application\chrome.exe"
    if (-not (Test-Path $chrome)) {
        $chrome = "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
    }
    if (Test-Path $chrome) {
        Write-Host "→ Öffne Chrome im App-Modus …"
        $profileDir = Join-Path $env:LOCALAPPDATA "WohnungsRadarChrome"
        Start-Process -FilePath $chrome -ArgumentList `
            "--app=$url", "--new-window", "--user-data-dir=`"$profileDir`""
    } else {
        Write-Host "⚠  chrome.exe nicht gefunden. Öffne manuell: $url" -ForegroundColor Yellow
    }
}

Write-Host "Wohnungs-Radar läuft auf $url"

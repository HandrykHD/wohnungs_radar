<#
    Wohnungs-Radar bei der Windows-Anmeldung automatisch starten.

    Registriert eine Aufgabe im Windows Task Scheduler, die start.ps1 beim Login
    (minimiert, im Hintergrund) ausführt. Einmal in PowerShell aufrufen:

        powershell -ExecutionPolicy Bypass -File .\install-autostart.ps1

    Entfernen:  schtasks /delete /tn "WohnungsRadar" /f
#>

$start = Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) "start.ps1"
$action = "powershell.exe -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$start`""

schtasks /create /tn "WohnungsRadar" /tr $action /sc onlogon /rl limited /f

Write-Host "Fertig. Der Radar startet ab dem nächsten Login automatisch."
Write-Host "Sofort testen:  schtasks /run /tn WohnungsRadar"

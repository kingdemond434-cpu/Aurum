<#
    Full teardown of the Aurum desk on Windows.

    ORDER MATTERS AND IT IS THE WHOLE POINT OF THIS SCRIPT.

    AurumSignalDesk-Watchdog and AurumSignalDesk-SelfHeal exist to notice the
    desk is not running and start it again. Killing python.exe first just hands
    them something to do — within a minute the desk is back and it looks like
    the kill silently failed. The supervisors are disabled FIRST, every time.

    Stops processes only. Nothing is uninstalled and no data, ledger, state or
    repository file is touched: the ledger is the only record of what the desk
    predicted, and a teardown that destroys evidence cannot be undone.

    RUN AS ADMINISTRATOR:
        powershell -ExecutionPolicy Bypass -File deploy\kill_aurum.ps1
    Add -WhatIf to see what it would do without doing it.
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param()

# Supervisors first, in this order. Anything that can restart the desk goes
# before anything that runs it.
$Supervisors = @(
    'AurumSignalDesk-Watchdog',
    'AurumSignalDesk-SelfHeal',
    'AurumSignalDesk-Update'
)

$Workers = @(
    'AurumSignalDesk',
    'AurumSignalDesk-Cycle',
    'AurumSignalDesk-VantageSpread',
    'MT5-Shadow',
    'MT5-ShadowSync',
    'MT5-QQuantGatesCertify',
    'Aurum-Sync'
)

function Stop-AurumTask {
    param([string]$Name)
    $q = schtasks /Query /TN $Name /FO CSV 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host ("  {0,-34} not present" -f $Name) -ForegroundColor DarkGray
        return
    }
    if ($PSCmdlet.ShouldProcess($Name, 'disable and end scheduled task')) {
        # /Change /DISABLE stops it firing again; /End kills the run in flight.
        # Both are needed: disabling alone leaves a running instance alive.
        schtasks /Change /TN $Name /DISABLE  2>&1 | Out-Null
        schtasks /End    /TN $Name           2>&1 | Out-Null
        Write-Host ("  {0,-34} disabled + ended" -f $Name) -ForegroundColor Yellow
    }
}

Write-Host "`n[1/4] Disabling supervisors (these restart the desk)" -ForegroundColor Cyan
foreach ($t in $Supervisors) { Stop-AurumTask $t }

Write-Host "`n[2/4] Disabling workers" -ForegroundColor Cyan
foreach ($t in $Workers) { Stop-AurumTask $t }

Write-Host "`n[3/4] Killing processes still holding the desk" -ForegroundColor Cyan
# Matched on command line, not image name: killing every python.exe on the box
# would take out anything else the user is running.
$patterns = 'run_desk\.py|aurum_cycle\.py|signals_capture\.py|golddesk|AurumSignalDesk'
$procs = Get-CimInstance Win32_Process |
    Where-Object { $_.CommandLine -and $_.CommandLine -match $patterns }

if (-not $procs) {
    Write-Host "  no matching processes" -ForegroundColor DarkGray
} else {
    foreach ($p in $procs) {
        $short = ($p.CommandLine -replace '\s+', ' ')
        if ($short.Length -gt 90) { $short = $short.Substring(0, 90) + '...' }
        if ($PSCmdlet.ShouldProcess("PID $($p.ProcessId)", 'kill')) {
            Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
            Write-Host ("  killed PID {0}  {1}" -f $p.ProcessId, $short) -ForegroundColor Yellow
        }
    }
}

Write-Host "`n[4/4] Verifying" -ForegroundColor Cyan
Start-Sleep -Seconds 3
$left = Get-CimInstance Win32_Process |
    Where-Object { $_.CommandLine -and $_.CommandLine -match $patterns }
$enabled = @()
foreach ($t in ($Supervisors + $Workers)) {
    $csv = schtasks /Query /TN $t /FO CSV /V 2>$null
    if ($LASTEXITCODE -eq 0 -and $csv -match 'Ready|Running') { $enabled += $t }
}

if (-not $left -and -not $enabled) {
    Write-Host "`nAURUM IS DOWN. No desk processes, no enabled tasks.`n" -ForegroundColor Green
} else {
    Write-Host "`nNOT FULLY DOWN:" -ForegroundColor Red
    foreach ($p in $left)    { Write-Host "  still running: PID $($p.ProcessId)" -ForegroundColor Red }
    foreach ($t in $enabled) { Write-Host "  still enabled: $t" -ForegroundColor Red }
    Write-Host "  Re-run as Administrator; schtasks silently no-ops without elevation.`n"
}

Write-Host "MT5 itself was NOT touched. The desk is advisory and places no orders," -ForegroundColor DarkGray
Write-Host "so any open position is yours and is still open. Close it in the terminal" -ForegroundColor DarkGray
Write-Host "if that is what you want.`n" -ForegroundColor DarkGray
Write-Host "To bring it back: schtasks /Change /TN <name> /ENABLE" -ForegroundColor DarkGray

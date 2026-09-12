<#
    Remove Aurum from this machine. One script, run once, done.

    This is the "I want it gone" version, not the "pause it" version
    (kill_aurum.ps1). Scheduled tasks are DELETED, not disabled, and anything
    that relaunches MT5 or the desk at login is removed.

    WHY MT5 KEEPS POPPING UP. It is not MT5 doing it. MT5-Shadow,
    MT5-ShadowSync and MT5-QQuantGatesCertify start the terminal on a schedule,
    and the watchdog restarts whatever it finds stopped. Closing the window
    only lasts until the next trigger. That is why the tasks are deleted first
    and the terminal killed second — the other order just loops.

    WHAT THIS DOES NOT TOUCH
      - Your MT5 installation and its login, charts and templates. This closes
        the terminal; it does not uninstall it. You can open it normally after.
      - Any open trade. Aurum was advisory and never placed an order, so
        whatever is open is yours, sitting at the broker with its own stop.
        Closing the terminal does not close it. If you want it flat, close it
        in MT5 or with your broker before running this.
      - The C:\Aurum folder, ledger and git history. Deleting those is
        irreversible and is one command, printed at the end, if you want it.

    RUN AS ADMINISTRATOR:
        powershell -ExecutionPolicy Bypass -File deploy\purge_aurum.ps1
    Preview first with:  -WhatIf
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    # Leave MT5 running (only remove Aurum's hold on it).
    [switch]$KeepMt5
)

# Supervisors first: these restart everything else.
$Tasks = @(
    'AurumSignalDesk-Watchdog',
    'AurumSignalDesk-SelfHeal',
    'AurumSignalDesk-Update',
    'AurumSignalDesk',
    'AurumSignalDesk-Cycle',
    'AurumSignalDesk-VantageSpread',
    'MT5-Shadow',
    'MT5-ShadowSync',
    'MT5-QQuantGatesCertify',
    'Aurum-Sync'
)

Write-Host "`n[1/5] Deleting scheduled tasks" -ForegroundColor Cyan
foreach ($t in $Tasks) {
    schtasks /Query /TN $t /FO CSV 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Host ("  {0,-34} not present" -f $t) -ForegroundColor DarkGray
        continue
    }
    if ($PSCmdlet.ShouldProcess($t, 'end and delete scheduled task')) {
        schtasks /End    /TN $t          2>&1 | Out-Null   # stop the run in flight
        schtasks /Delete /TN $t /F       2>&1 | Out-Null   # then remove it for good
        Write-Host ("  {0,-34} deleted" -f $t) -ForegroundColor Yellow
    }
}

# Anything Aurum registered to start at login. Catch-all scan rather than a
# fixed list, because an installer may have written more than one.
Write-Host "`n[2/5] Removing login/startup entries" -ForegroundColor Cyan
$runKeys = @(
    'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run',
    'HKLM:\Software\Microsoft\Windows\CurrentVersion\Run',
    'HKCU:\Software\Microsoft\Windows\CurrentVersion\RunOnce'
)
$found = $false
foreach ($k in $runKeys) {
    if (-not (Test-Path $k)) { continue }
    $props = Get-ItemProperty -Path $k -ErrorAction SilentlyContinue
    foreach ($p in $props.PSObject.Properties) {
        if ($p.Name -like 'PS*') { continue }
        if ("$($p.Name) $($p.Value)" -match 'aurum|run_desk|golddesk|AurumSignalDesk') {
            $found = $true
            if ($PSCmdlet.ShouldProcess("$k\$($p.Name)", 'remove run key')) {
                Remove-ItemProperty -Path $k -Name $p.Name -Force -ErrorAction SilentlyContinue
                Write-Host "  removed $k\$($p.Name)" -ForegroundColor Yellow
            }
        }
    }
}
foreach ($dir in @([Environment]::GetFolderPath('Startup'),
                   [Environment]::GetFolderPath('CommonStartup'))) {
    if (-not $dir -or -not (Test-Path $dir)) { continue }
    Get-ChildItem $dir -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match 'aurum|metatrader|mt5' } | ForEach-Object {
            $found = $true
            if ($PSCmdlet.ShouldProcess($_.FullName, 'delete startup shortcut')) {
                Remove-Item $_.FullName -Force -ErrorAction SilentlyContinue
                Write-Host "  removed shortcut $($_.Name)" -ForegroundColor Yellow
            }
        }
}
if (-not $found) { Write-Host "  none found" -ForegroundColor DarkGray }

Write-Host "`n[3/5] Killing desk processes" -ForegroundColor Cyan
# Matched on command line, so unrelated python on this box is left alone.
$deskPat = 'run_desk\.py|aurum_cycle\.py|signals_capture\.py|golddesk|AurumSignalDesk'
$procs = Get-CimInstance Win32_Process |
    Where-Object { $_.CommandLine -and $_.CommandLine -match $deskPat }
if (-not $procs) { Write-Host "  none running" -ForegroundColor DarkGray }
foreach ($p in $procs) {
    if ($PSCmdlet.ShouldProcess("PID $($p.ProcessId)", 'kill desk process')) {
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
        Write-Host "  killed PID $($p.ProcessId)" -ForegroundColor Yellow
    }
}

Write-Host "`n[4/5] Closing MetaTrader" -ForegroundColor Cyan
if ($KeepMt5) {
    Write-Host "  -KeepMt5 given; leaving the terminal open" -ForegroundColor DarkGray
} else {
    $mt5 = Get-Process -Name 'terminal64', 'terminal', 'metaeditor64', 'metatester64' `
                       -ErrorAction SilentlyContinue
    if (-not $mt5) { Write-Host "  not running" -ForegroundColor DarkGray }
    foreach ($p in $mt5) {
        if ($PSCmdlet.ShouldProcess("$($p.ProcessName) (PID $($p.Id))", 'close')) {
            # Ask politely first so MT5 flushes its own state, then insist.
            $null = $p.CloseMainWindow()
            Start-Sleep -Milliseconds 1500
            if (-not $p.HasExited) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue }
            Write-Host "  closed $($p.ProcessName) (PID $($p.Id))" -ForegroundColor Yellow
        }
    }
}

Write-Host "`n[5/5] Verifying" -ForegroundColor Cyan
Start-Sleep -Seconds 3
$leftTasks = @()
foreach ($t in $Tasks) {
    schtasks /Query /TN $t /FO CSV 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { $leftTasks += $t }
}
$leftProcs = @(Get-CimInstance Win32_Process |
    Where-Object { $_.CommandLine -and $_.CommandLine -match $deskPat })
$leftMt5 = if ($KeepMt5) { @() } else {
    @(Get-Process -Name 'terminal64','terminal','metaeditor64','metatester64' -ErrorAction SilentlyContinue)
}

if (-not $leftTasks -and -not $leftProcs -and -not $leftMt5) {
    Write-Host "`nAURUM IS GONE. No tasks, no processes, terminal closed." -ForegroundColor Green
    Write-Host "Nothing will start it again at login or on a timer.`n" -ForegroundColor Green
} else {
    Write-Host "`nSTILL PRESENT:" -ForegroundColor Red
    foreach ($t in $leftTasks) { Write-Host "  task    $t" -ForegroundColor Red }
    foreach ($p in $leftProcs) { Write-Host "  process PID $($p.ProcessId)" -ForegroundColor Red }
    foreach ($p in $leftMt5)   { Write-Host "  mt5     $($p.ProcessName) PID $($p.Id)" -ForegroundColor Red }
    Write-Host "`n  Almost always elevation: schtasks and Stop-Process no-op silently" -ForegroundColor Red
    Write-Host "  without Administrator. Re-run from an elevated PowerShell.`n" -ForegroundColor Red
}

Write-Host "MT5 is closed, not uninstalled -- open it normally whenever you like." -ForegroundColor DarkGray
Write-Host "Any open trade is untouched and still at your broker.`n" -ForegroundColor DarkGray
Write-Host "The C:\Aurum folder is still on disk. If you want it gone too:" -ForegroundColor DarkGray
Write-Host "    Remove-Item -Recurse -Force C:\Aurum" -ForegroundColor DarkGray
Write-Host "That is irreversible and takes the ledger and git history with it.`n" -ForegroundColor DarkGray

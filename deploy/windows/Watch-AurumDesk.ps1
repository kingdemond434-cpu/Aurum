<#
.SYNOPSIS
    The AtLogOn task's blind spot: if the whole tree dies with no logon in between, nothing
    else notices. This does.

.DESCRIPTION
    THE GAP THIS CLOSES, PRECISELY

    AurumSignalDesk (Install-AurumStartup.ps1) triggers AtLogOn. Once running, its own supervisor
    loop (Start-AurumDesk.ps1) now survives a crash loop forever -- it plateaus and keeps
    retrying rather than exiting. Between the two, most failure modes are covered.

    One is not: if BOTH the supervisor and the python child are killed -- Task Manager, a bad
    Windows Update, an out-of-memory reaper, anything that takes out the whole process tree
    without a logon happening -- the AtLogOn trigger has nothing to fire on. It only fires AT
    LOGON. No logon, no relaunch, and the desk stays dead until a human notices and manually
    starts it or reboots the box. That silently defeats "24/7, never dying" the same way the
    missing autologon defeats reboot survival, just for a different triggering event.

    This is the standard fix for that gap: a SEPARATE, TIME-TRIGGERED task that does nothing
    but check "is the desk process actually alive right now", and (re)launches the supervisor
    if not. It is deliberately NOT the thing that runs the desk -- Start-AurumDesk.ps1 keeps
    that job -- this only answers "should something be running, and is it".

    WHY POLL RATHER THAN RELY ON THE SUPERVISOR ALONE

    A supervisor cannot supervise its own death. Every failure mode this script exists for is
    one where Start-AurumDesk.ps1's own loop never gets a chance to run its recovery logic,
    because the process running that loop is gone. Only something OUTSIDE that process can
    notice its absence, which is what a second, independent, time-triggered task is for.

    WHY THIS DOES NOT DOUBLE-LAUNCH THE DESK

    It checks for a live `run_desk.py` process by command line before doing anything. If one is
    found -- healthy, restarting, or even mid-crash-loop under its own supervisor -- this exits
    immediately and does nothing. It only acts on TRUE ABSENCE: no python process with
    run_desk.py in its command line anywhere on the box. It does not check heartbeat freshness
    or supervisor.log, on purpose: a DEGRADED desk retrying every five minutes is still working
    as designed and must not be treated as a reason to pile a second supervisor tree on top of
    the first.

.PARAMETER DeskRoot
    The Aurum checkout. Defaults to this script's grandparent, correct by construction.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File deploy\windows\Watch-AurumDesk.ps1
#>
[CmdletBinding()]
param(
    [string] $DeskRoot = (Split-Path -Parent (Split-Path -Parent $(
                 if ($PSScriptRoot) { $PSScriptRoot }
                 else { Split-Path -Parent $MyInvocation.MyCommand.Path })))
)

$ErrorActionPreference = "Stop"

if ($PSVersionTable.PSVersion.Major -lt 3) {
    Write-Host ("FATAL: PowerShell $($PSVersionTable.PSVersion) found; this script needs 3.0 " +
               "or later. Install Windows Management Framework 4.0+, or a newer PowerShell " +
               "from https://aka.ms/PSWindows, then retry.")
    exit 1
}

$logDir = Join-Path $DeskRoot "logs"
$log = Join-Path $logDir "watchdog.log"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

function Write-Watchdog($msg) {
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Add-Content -Path $log -Value $line -Encoding utf8
}

$alive = @(Get-CimInstance Win32_Process -Filter "Name like '%python%'" -ErrorAction SilentlyContinue |
           Where-Object { $_.CommandLine -like "*run_desk.py*" })
if ($alive.Count -gt 0) {
    # The common case, deliberately silent in the log -- a watchdog that writes every five
    # minutes even when nothing is wrong trains whoever reads it to stop reading it.
    exit 0
}

# Same check for the SUPERVISOR itself. A supervisor that is up but between attempts (inside its
# own Start-Sleep backoff) is legitimately not running python right now, and relaunching a
# second supervisor on top of it would violate the "do not double-launch" contract above.
$supervisorAlive = @(Get-CimInstance Win32_Process -Filter "Name = 'powershell.exe'" -ErrorAction SilentlyContinue |
                     Where-Object { $_.CommandLine -like "*Start-AurumDesk*" })
if ($supervisorAlive.Count -gt 0) {
    exit 0
}

Write-Watchdog "NEITHER the desk nor its supervisor is running. Relaunching AurumSignalDesk."
try {
    Start-ScheduledTask -TaskName "AurumSignalDesk" -ErrorAction Stop
    Write-Watchdog "Start-ScheduledTask issued for AurumSignalDesk."
} catch {
    Write-Watchdog "FAILED to start AurumSignalDesk: $_"
}

<#
.SYNOPSIS
    Make the Aurum desk survive a VPS reboot. Registers a Scheduled Task; verifies it registered.

.DESCRIPTION
    WHAT SURVIVING A REBOOT ACTUALLY REQUIRES, AND WHY IT IS NOT ONE SETTING

    There are two different failures people mean by "it didn't come back":

      1. RDP DISCONNECT. You close the window; the session keeps running and so does the desk.
         Nothing is needed for this -- it already works. Logging off, however, is not a
         disconnect: it ends the session and kills the desk.
      2. REBOOT. Windows Update, a host migration, a crash. The session is gone.

    Only (2) needs this script, and it needs BOTH halves below. Installing one and not the other
    is the usual outcome, and it produces a box that looks configured and comes back dead.

    HALF ONE -- THE TASK (this script)

    A Scheduled Task with the AtLogOn trigger, running the supervisor.

    HALF TWO -- A DESKTOP SESSION TO RUN IN (NOT this script; see below)

    MT5 IS A GUI APPLICATION AND CANNOT RUN WITHOUT AN INTERACTIVE DESKTOP. This is the fact
    that shapes everything else. Task Scheduler's "run whether user is logged on or not" option
    exists and is the right answer for headless work -- it is the WRONG answer here, because it
    runs in session 0 with no desktop, and `mt5.initialize()` will fail there every time. A task
    configured that way looks correct in the UI and never produces a signal.

    So the box must reach a logged-on desktop by itself after a reboot, which means autologon.
    That is a real security decision and this script deliberately DOES NOT MAKE IT FOR YOU:

      - Autologon means anyone who can reach the console or RDP gets a logged-in session on a
        box holding your MT5 terminal and your Telegram bot token.
      - The credential is recoverable by any local administrator regardless of how it is stored.
        Sysinternals `Autologon64.exe` puts it in an LSA secret rather than plaintext in the
        registry, which is better and is not the same as safe.

    Set it with:  Autologon64.exe  (https://learn.microsoft.com/sysinternals/downloads/autologon)
    Or decide the risk is not worth it and log in by hand after a reboot -- a defensible choice
    for an advisory desk that places no orders. What is NOT defensible is assuming it is handled.

    WHY A TASK RATHER THAN THE STARTUP FOLDER

    The Startup folder runs a thing once, with no restart, no exit-code visibility, and no way
    to query whether it is armed. A Scheduled Task can be inspected (`Get-ScheduledTask`),
    triggered on demand for testing, restarted by Windows if it fails, and reports its last
    result -- all of which matter when the question is "is this box actually configured", asked
    from somewhere other than the box.

.PARAMETER DeskRoot
    The Aurum checkout. Defaults to this script's grandparent, correct by construction.

.PARAMETER TaskName
    Scheduled Task name. Re-running with the same name replaces it, so this is idempotent.

.PARAMETER Remove
    Unregister the task and exit.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File deploy\windows\Install-AurumStartup.ps1
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File deploy\windows\Install-AurumStartup.ps1 -Remove
#>
[CmdletBinding()]
param(
    [string] $DeskRoot,
    [string] $TaskName = "AurumSignalDesk",
    [string[]] $DeskArgs = @("--shadow", "--provider", "claudecode:claude-opus-5",
                             "--numeric-only", "--expect-broker", "Fusion"),
    [switch] $Remove
)

$ErrorActionPreference = "Stop"

# $PSScriptRoot IS EMPTY WHEN USED AS A PARAMETER DEFAULT VALUE on Windows PowerShell 5.1 --
# defaults are bound before the script's own automatic variables are fully established, so
# `$DeskRoot = (Split-Path ... $PSScriptRoot)` silently defaulted to an empty string and every
# path built from it broke. Resolved here instead, in the script body, where $PSScriptRoot is
# reliably populated, with a fallback chain for anything that still leaves it unset.
if (-not $DeskRoot) {
    $scriptDir = $PSScriptRoot
    if (-not $scriptDir) { $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path }
    if (-not $scriptDir) { $scriptDir = Split-Path -Parent $PSCommandPath }
    if (-not $scriptDir) {
        throw "cannot determine this script's own location to derive -DeskRoot; pass it explicitly"
    }
    $DeskRoot = Split-Path -Parent (Split-Path -Parent $scriptDir)
}

if ($Remove) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "removed scheduled task '$TaskName'"
    } else {
        Write-Host "no scheduled task named '$TaskName' -- nothing to remove"
    }
    exit 0
}

$supervisor = Join-Path $PSScriptRoot "Start-AurumDesk.ps1"
if (-not (Test-Path $supervisor)) { throw "supervisor not found at $supervisor" }
if (-not (Test-Path (Join-Path $DeskRoot "run_desk.py"))) {
    throw "run_desk.py not found under $DeskRoot -- pass -DeskRoot with the Aurum checkout path"
}

$argLiteral = ($DeskArgs | ForEach-Object { "'$_'" }) -join ","
$psArgs = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden " +
          "-File `"$supervisor`" -DeskRoot `"$DeskRoot`" -DeskArgs $argLiteral"

$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $psArgs `
                                  -WorkingDirectory $DeskRoot

# AtLogOn, NOT AtStartup: see the note above. AtStartup runs in session 0, where MT5 has no
# desktop and mt5.initialize() cannot succeed.
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

# Interactive, and running as the logged-on user rather than SYSTEM -- same reason. SYSTEM has
# no access to this user's MT5 terminal profile or its saved broker connection.
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
                                        -LogonType Interactive -RunLevel Limited

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew

# ExecutionTimeLimit Zero = no limit. The DEFAULT IS THREE DAYS, and Windows kills the task
# silently when it expires -- a desk that dies every 72 hours with no error anywhere is exactly
# the kind of fault that gets misread as a market gone quiet.
# MultipleInstances IgnoreNew stops a second supervisor spawning on a re-logon and racing the
# first for the MT5 terminal.

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
                       -Principal $principal -Settings $settings -Force | Out-Null

# VERIFY IT REGISTERED, rather than trusting that Register- did not throw. "Installed" is not a
# status; the check is whether the thing is there afterwards.
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) { throw "task '$TaskName' did not register" }

Write-Host ""
Write-Host "REGISTERED: $TaskName"
Write-Host "  runs      : $supervisor"
Write-Host "  desk root : $DeskRoot"
Write-Host "  args      : $($DeskArgs -join ' ')"
Write-Host "  trigger   : at logon of $env:USERDOMAIN\$env:USERNAME"
Write-Host "  state     : $($task.State)"
Write-Host ""
Write-Host "STILL REQUIRED -- the task cannot do this part:"
Write-Host "  Autologon, so a reboot reaches a desktop for MT5 to draw on."
Write-Host "  Sysinternals Autologon64.exe. Read the security note in this script first."
Write-Host ""
Write-Host "TEST IT WITHOUT REBOOTING:"
Write-Host "  Start-ScheduledTask -TaskName $TaskName"
Write-Host "  Get-Content '$DeskRoot\logs\supervisor_heartbeat.json'"
Write-Host ""
Write-Host "THE ONLY TEST THAT PROVES IT: reboot the box and confirm a Telegram message"
Write-Host "arrives without you touching anything."

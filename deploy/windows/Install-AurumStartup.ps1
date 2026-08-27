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
    # MUST STAY IDENTICAL TO Start-AurumDesk.ps1's -DeskArgs DEFAULT, and it silently did not.
    # This list is joined into the scheduled task's action string, and Start-AurumDesk.ps1 treats
    # a non-empty -DeskArgsJoined as an OVERRIDE of its own default -- so whatever is written
    # here is what the desk actually runs on every boot, and the other file's default is dead
    # text the moment the task exists.
    #
    # It drifted: 43dd2b8 added --wake-every-bar and --universe to Start-AurumDesk.ps1 and not
    # here, so the installed task kept launching without them. The desk's own startup banner
    # reported it truthfully ("opportunity set : single read") and nothing else did, because a
    # missing capture flag is not an error -- it is just less capture, forever, quietly. Caught
    # 2026-08-27 when the operator asked why breadth was not what the commit said it was.
    #
    # --universe is the one that costs nothing to keep: the brief (levels, macro, context) is
    # the expensive half of a read and is sent whether the model answers with one proposition or
    # twelve, so enumerating the set raises capture per unit of quota rather than raising call
    # frequency. --wake-every-bar does raise frequency, and its cost is recorded in 43dd2b8.
    [string[]] $DeskArgs = @("--shadow", "--provider", "claudecode:claude-opus-5",
                             "--numeric-only", "--expect-broker", "Fusion",
                             "--wake-every-bar", "--universe", "--effort", "high"),
    [switch] $Remove
)

$ErrorActionPreference = "Stop"

# The ScheduledTasks cmdlets this script depends on (Get-/Register-/Unregister-ScheduledTask,
# New-ScheduledTaskAction/-Trigger/-Principal/-Settings) ship with PowerShell 3.0+ on Windows
# 8/Server 2012 and later. Failing here, once, beats failing on the first cmdlet call with
# "the term 'Get-ScheduledTask' is not recognized", which reads like a missing module rather
# than an old host.
if ($PSVersionTable.PSVersion.Major -lt 3) {
    Write-Host ("FATAL: PowerShell $($PSVersionTable.PSVersion) found; this script needs 3.0 " +
               "or later (the ScheduledTasks cmdlets do not exist before it). Install Windows " +
               "Management Framework 4.0+, or a newer PowerShell from " +
               "https://aka.ms/PSWindows, then retry.")
    exit 1
}

# $PSScriptRoot is empty when referenced INSIDE a param() default value on Windows PowerShell
# 5.1 -- defaults bind before the script's own automatic variables are fully established, which
# is exactly the "Cannot bind argument to parameter 'Path' because it is an empty string" hit
# live on the VPS. A fallback inside the param default does not fix this: whatever it falls back
# to is evaluated at that same early moment and is just as unreliable there. Resolved here
# instead, in the script body, where $PSScriptRoot (and everything else) is reliably populated.
$ScriptRoot = $PSScriptRoot
if (-not $ScriptRoot) { $ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path }
if (-not $ScriptRoot) { $ScriptRoot = Split-Path -Parent $PSCommandPath }
if (-not $ScriptRoot) {
    throw "cannot determine this script's own location to derive -DeskRoot; pass it explicitly"
}
if (-not $DeskRoot) {
    $DeskRoot = Split-Path -Parent (Split-Path -Parent $ScriptRoot)
}

$WatchdogTaskName = "$TaskName-Watchdog"

if ($Remove) {
    foreach ($t in @($TaskName, $WatchdogTaskName, "$TaskName-Cycle",
                     "$TaskName-VantageSpread")) {
        if (Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $t -Confirm:$false
            Write-Host "removed scheduled task '$t'"
        } else {
            Write-Host "no scheduled task named '$t' -- nothing to remove"
        }
    }
    exit 0
}

$supervisor = Join-Path $ScriptRoot "Start-AurumDesk.ps1"
if (-not (Test-Path $supervisor)) { throw "supervisor not found at $supervisor" }
if (-not (Test-Path (Join-Path $DeskRoot "run_desk.py"))) {
    throw "run_desk.py not found under $DeskRoot -- pass -DeskRoot with the Aurum checkout path"
}

# THIS USED TO BE ($DeskArgs | ForEach-Object { "'$_'" }) -join "," -- e.g.
# '--shadow','--provider','claudecode:claude-opus-5','--numeric-only','--expect-broker','Fusion'
# -- and it silently produced a desk that could not start under the scheduled task while working
# perfectly when run by hand, for four consecutive fast failures before the cause was visible.
#
# THE MECHANISM: that comma-joined text was embedded into $psArgs, the raw command line for a
# BRAND NEW powershell.exe process launched by the scheduled task action. Comma is the array
# CONSTRUCTION OPERATOR only when PowerShell's own LANGUAGE PARSER reads script or console text
# -- it means nothing to the OS-level command-line tokenizer that splits a NEW process's argv on
# WHITESPACE (honouring quotes). Because `-join ","` inserts no spaces, the entire blob --
# quotes, commas and all -- contained zero whitespace and arrived as ONE token. -DeskArgs (typed
# [string[]]) bound that single literal string as a one-element array, and run_desk.py's argparse
# then reported it verbatim as one unrecognised argument -- which is exactly the comma-and-quote
# text seen in the failure, not six separate flags.
#
# A naive fix (space-join instead of comma-join) trades this bug for a different one: several of
# these values themselves start with `--` (--provider, --numeric-only, --expect-broker), and
# PowerShell's own -File parameter binder can mistake a dash-prefixed TOKEN for an attempt to
# specify a new named parameter rather than a continuation of -DeskArgs's array. Whitespace-
# splitting the values is what creates that ambiguity in the first place.
#
# So this now travels as ONE opaque, properly double-quoted string -- immune to both problems,
# since there is only one token for -File's parser to bind to -DeskArgsJoined, and its content
# (including every embedded dash) is never re-examined as separate command-line tokens.
# Start-AurumDesk.ps1 splits it back into an array in its own script body. "|" is the delimiter
# because none of the values this desk currently passes can contain one; if a future argument
# value legitimately needs a literal "|", change the delimiter on both sides together.
$argJoined = $DeskArgs -join "|"
$psArgs = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden " +
          "-File `"$supervisor`" -DeskRoot `"$DeskRoot`" -DeskArgsJoined `"$argJoined`""

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

# THE WATCHDOG. AtLogOn covers "the box rebooted or you logged back in". It does NOT cover "the
# whole process tree got killed with no logon in between" -- Task Manager, an update, an OOM
# reaper -- because AtLogOn has nothing to fire on until the next logon. This is the standard
# fix: a SEPARATE, time-triggered task that checks whether the desk (or its supervisor) is
# actually alive, and relaunches AurumSignalDesk only on true absence. See Watch-AurumDesk.ps1's
# own header for why it cannot double-launch a healthy or merely-degraded desk.
#
# New-ScheduledTaskTrigger has no direct "every N minutes forever" trigger type -- a One-time
# trigger with a repetition interval and a duration far longer than this desk will run
# unattended is the standard construction for a recurring task via this cmdlet family.
$watchdogScript = Join-Path $ScriptRoot "Watch-AurumDesk.ps1"
if (-not (Test-Path $watchdogScript)) { throw "watchdog not found at $watchdogScript" }
$wdArgs = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden " +
          "-File `"$watchdogScript`" -DeskRoot `"$DeskRoot`""
$wdAction = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $wdArgs `
                                    -WorkingDirectory $DeskRoot
# REPETITION DURATION IS 10 YEARS, NOT [TimeSpan]::MaxValue. MaxValue
# serialises to the ISO-8601 duration P99999999DT23H59M59S, which the Task
# Scheduler service rejects outright, and the failure is the worst shape
# available -- it happens at REGISTRATION:
#
#   Register-ScheduledTask : The task XML contains a value which is incorrectly
#   formatted or out of range. (8,42):Duration:P99999999DT23H59M59S
#
# So AurumSignalDesk installed fine, the installer looked like it worked, and
# ONLY the watchdog silently did not exist -- leaving the desk with exactly the
# blind spot the watchdog was written to cover. Observed on the live Windows
# VPS, 2026-08-22. 3650 days is indefinite for any practical purpose and
# serialises to a duration the service accepts.
$wdTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
                                      -RepetitionInterval (New-TimeSpan -Minutes 5) `
                                      -RepetitionDuration (New-TimeSpan -Days 3650)
$wdPrincipal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
                                          -LogonType Interactive -RunLevel Limited
$wdSettings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 4) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $WatchdogTaskName -Action $wdAction -Trigger $wdTrigger `
                       -Principal $wdPrincipal -Settings $wdSettings -Force | Out-Null
$wdTask = Get-ScheduledTask -TaskName $WatchdogTaskName -ErrorAction SilentlyContinue
if (-not $wdTask) { throw "task '$WatchdogTaskName' did not register" }

# ---- THE EXECUTION VENUE'S SPREAD, MEASURED RATHER THAN ASSERTED --------------------------
#
# The desk reads prices from Fusion and the operator executes on Vantage, so every expectancy
# figure was being priced against a spread that is never paid -- the exact warning run_desk.py
# prints at every launch. calibrate_spread.py cannot close it here: it measures from the
# ledger, and the ledger holds the FEED's quotes, so it would produce a precise per-session
# number for the wrong broker.
#
# Sampled every 20 minutes rather than once: gold's ASIA book and its OVERLAP book are
# different markets and rollover is different again, and venue.calibrate refuses any session
# with fewer than 100 samples rather than reporting a median of twenty. The archive is
# cumulative, so the profile converges instead of re-guessing from the last two minutes.
#
# CONSERVATIVE (p75) while the archive is thin, deliberately: being slightly pessimistic about
# cost is a far cheaper error than being optimistic, because optimism shows up as trades that
# looked positive and were not.
$spreadScript = Join-Path $DeskRoot "sample_vantage_spread.py"
if (Test-Path $spreadScript) {
    $SpreadTaskName = "$TaskName-VantageSpread"
    $py = Join-Path $DeskRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path $py)) { $py = "py" }
    $spreadLog = Join-Path $DeskRoot "logs\vantage_spread.log"
    $spreadCmd = "/d /s /c `"`"$py`" `"$spreadScript`" --seconds 90 --statistic conservative " +
                 ">> `"$spreadLog`" 2>&1`""
    $spAction = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $spreadCmd `
                                        -WorkingDirectory $DeskRoot
    $spTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
                                          -RepetitionInterval (New-TimeSpan -Minutes 20) `
                                          -RepetitionDuration (New-TimeSpan -Days 3650)
    $spPrincipal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
                                              -LogonType Interactive -RunLevel Limited
    $spSettings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 5) -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $SpreadTaskName -Action $spAction -Trigger $spTrigger `
                           -Principal $spPrincipal -Settings $spSettings -Force | Out-Null
    $spTask = Get-ScheduledTask -TaskName $SpreadTaskName -ErrorAction SilentlyContinue
    if (-not $spTask) { throw "task '$SpreadTaskName' did not register" }
} else {
    Write-Host "  [SKIP] $TaskName-VantageSpread : sample_vantage_spread.py not found"
}

# ---------------------------------------------------------------------------
# THE LEARNING CYCLE. Everything that turns yesterday's results into tomorrow's
# behaviour lives in aurum_cycle.py: decay detection over every mechanism,
# missed-money pricing of what each restriction refused, the management
# counterfactual, the stop autopsy, the growth re-solve.
#
# NOTHING ON THIS BOX EVER RAN IT. The only launcher in the repo was
# deploy/aurum-cycle.service -- a systemd unit for /opt/aurum, on Linux. On a
# Windows desk that file is inert, so every self-correction step was written,
# tested, correct and executed by nobody (III.16: a capability is done when
# something RUNS it on a schedule and the run leaves an artifact).
#
# That is the difference between a desk that CAN learn and one that DOES. The
# operator asked why it was not improving on its own; this is the answer.
#
# 22:40 UTC daily. THE TIME IS A DEPENDENCY, NOT A PREFERENCE, and it is third
# in a chain that only works in order:
#
#   21:45  quant's daily_cycle.py step 4 writes desks\mt5\reports\aurum_findings.jsonl
#   22:15  Aurum-Sync (registered by quant's installer) carries it into inbox\quant_findings.jsonl
#   22:40  THIS task runs step_absorb, which reads that inbox
#
# This was first set to 22:10 -- FIVE MINUTES BEFORE the sync that feeds it. The
# cycle would have read a stale inbox every night and absorbed every quant
# finding exactly one day late, silently, because "0 new findings" reads
# identically to the quant desk having learned nothing. 22:40 leaves room for
# the sync's own retry budget (RestartCount 3 at 5-minute intervals) to finish.
#
# Also after the New York close and before the Asia open, so it reads a settled
# day and never competes with the desk for the MT5 terminal.
$cycleScript = Join-Path $DeskRoot "aurum_cycle.py"
if (Test-Path $cycleScript) {
    $CycleTaskName = "$TaskName-Cycle"
    $cpy = Join-Path $DeskRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path $cpy)) { $cpy = "py" }
    $cycleLog = Join-Path $DeskRoot "logs\cycle.log"
    $cycleCmd = "/d /s /c `"`"$cpy`" `"$cycleScript`" >> `"$cycleLog`" 2>&1`""
    $cyAction = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $cycleCmd `
                                        -WorkingDirectory $DeskRoot
    $cyTrigger = New-ScheduledTaskTrigger -Daily -At ([datetime]::Today.AddHours(22).AddMinutes(40))
    $cyPrincipal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
                                              -LogonType Interactive -RunLevel Limited
    # StartWhenAvailable matters here specifically: a daily task that fires while
    # the box is off is otherwise SILENTLY SKIPPED, and a learning cycle that
    # misses days without saying so is the same defect one level up.
    $cySettings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 45) -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $CycleTaskName -Action $cyAction -Trigger $cyTrigger `
                           -Principal $cyPrincipal -Settings $cySettings -Force | Out-Null
    $cyTask = Get-ScheduledTask -TaskName $CycleTaskName -ErrorAction SilentlyContinue
    if (-not $cyTask) { throw "task '$CycleTaskName' did not register" }
} else {
    Write-Host "  [SKIP] $TaskName-Cycle : aurum_cycle.py not found"
}

Write-Host ""

Write-Host "REGISTERED: $TaskName"
Write-Host "  runs      : $supervisor"
Write-Host "  desk root : $DeskRoot"
Write-Host "  args      : $($DeskArgs -join ' ')"
Write-Host "  trigger   : at logon of $env:USERDOMAIN\$env:USERNAME"
Write-Host "  state     : $($task.State)"
Write-Host ""
if ($spTask) {
    Write-Host "REGISTERED: $SpreadTaskName"
    Write-Host "  runs      : $spreadScript"
    Write-Host "  trigger   : every 20 minutes, indefinitely"
    Write-Host "  purpose   : measure the EXECUTION venue's (Vantage) per-session spread, so"
    Write-Host "              expectancy stops being priced against Fusion's feed -- a cost"
    Write-Host "              this account never pays. Archive is cumulative; the profile"
    Write-Host "              converges. Refuses if the terminal is not Vantage."
    Write-Host "  state     : $($spTask.State)"
    Write-Host ""
}
Write-Host "REGISTERED: $TaskName-Cycle"
Write-Host "  runs      : $DeskRoot\aurum_cycle.py"
Write-Host "  trigger   : daily 22:40 (AFTER Aurum-Sync 22:15 delivers quant findings)"
Write-Host "  purpose   : the LEARNING loop -- decay, missed-money, management"
Write-Host "              counterfactual, stop autopsy, growth re-solve. Nothing"
Write-Host "              on Windows ever ran it: the only launcher in the repo"
Write-Host "              was a Linux systemd unit, so every self-correction"
Write-Host "              step was correct and executed by nobody."
Write-Host ""
Write-Host "REGISTERED: $WatchdogTaskName"
Write-Host "  runs      : $watchdogScript"
Write-Host "  trigger   : every 5 minutes, indefinitely"
Write-Host "  purpose   : relaunches $TaskName if BOTH it and the desk process have died with"
Write-Host "              no logon in between -- the one gap AtLogOn alone cannot cover"
Write-Host "  state     : $($wdTask.State)"
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

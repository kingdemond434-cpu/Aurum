<#
.SYNOPSIS
    Is this box actually configured, and is the desk actually alive? One command, honest answers.

.DESCRIPTION
    Every check here distinguishes three states, never two: OK, a named failure, and UNKNOWN.
    "I could not measure this" is a real answer and is not the same as "this is fine" -- the
    latter is what a status screen says by default, and it is how a box that has been dead for
    two days keeps looking configured.

    Run it after installing, after a reboot, and any time the Telegram channel goes quiet.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File deploy\windows\Get-AurumStatus.ps1
#>
[CmdletBinding()]
param(
    [string] $DeskRoot,
    [string] $TaskName = "AurumSignalDesk"
)

$ErrorActionPreference = "Continue"

# -notin, Get-ScheduledTask, Get-CimInstance and ConvertFrom-Json all need PowerShell 3.0+.
# Failing here, once, beats this script dying midway through its own checks with an error that
# looks like a missing module rather than an old host.
if ($PSVersionTable.PSVersion.Major -lt 3) {
    Write-Host ("FATAL: PowerShell $($PSVersionTable.PSVersion) found; this script needs 3.0 " +
               "or later. Install Windows Management Framework 4.0+, or a newer PowerShell " +
               "from https://aka.ms/PSWindows, then retry.")
    exit 1
}

# $PSScriptRoot is empty when referenced INSIDE a param() default value on Windows PowerShell
# 5.1 -- defaults bind before the script's own automatic variables are fully established, which
# is exactly the "Cannot bind argument to parameter 'Path' because it is an empty string" hit
# live on the VPS. A fallback inside the param default does not fix this: whatever it falls back
# to is evaluated at that same early moment and is just as unreliable there. Resolved here
# instead, in the script body, where $PSScriptRoot (and everything else) is reliably populated.
if (-not $DeskRoot) {
    $scriptDir = $PSScriptRoot
    if (-not $scriptDir) { $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path }
    if (-not $scriptDir) { $scriptDir = Split-Path -Parent $PSCommandPath }
    if (-not $scriptDir) {
        throw "cannot determine this script's own location to derive -DeskRoot; pass it explicitly"
    }
    $DeskRoot = Split-Path -Parent (Split-Path -Parent $scriptDir)
}

$fails = 0

function Report($name, $state, $detail) {
    $mark = switch ($state) { "OK" { "PASS" } "UNKNOWN" { "????" } default { "FAIL" } }
    "{0,-6} {1,-22} {2}" -f "[$mark]", $name, $detail | Write-Host
    if ($state -notin @("OK", "UNKNOWN")) { $script:fails++ }
}

Write-Host ""
Write-Host ("=" * 78)
Write-Host "AURUM STATUS -- $DeskRoot"
Write-Host ("=" * 78)

# --- the interpreter, because this is the one that bites first ---------------
# `python3` is the LINUX name and does not exist on Windows. Instructions written on Linux fail
# here with "not recognized as the name of a cmdlet", which reads like the script is missing
# rather than the interpreter being called by the wrong name. Naming the working command is
# more useful than reporting a boolean.
$venv = Join-Path $DeskRoot ".venv\Scripts\python.exe"
if (Test-Path $venv) {
    Report "python" "OK" "$venv (repo venv -- prefer this)"
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    Report "python" "OK" "py -3   (NOT 'python3' -- that name is Linux-only)"
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    Report "python" "OK" "python  (NOT 'python3' -- that name is Linux-only)"
} else {
    Report "python" "MISSING" "no interpreter found: no .venv, no 'py', no 'python' on PATH"
}

# --- the checkout ------------------------------------------------------------
if (Test-Path (Join-Path $DeskRoot "run_desk.py")) {
    Report "checkout" "OK" "run_desk.py present"
} else {
    Report "checkout" "MISSING" "no run_desk.py under $DeskRoot -- wrong -DeskRoot?"
}

# --- the scheduled task ------------------------------------------------------
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) {
    Report "startup task" "MISSING" "no task '$TaskName' -- run Install-AurumStartup.ps1"
} else {
    $info = Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction SilentlyContinue
    $last = if ($info) { $info.LastTaskResult } else { $null }
    $when = if ($info -and $info.LastRunTime.Year -gt 1999) { $info.LastRunTime } else { "never" }
    Report "startup task" "OK" "$TaskName state=$($task.State) lastRun=$when lastResult=$last"
}

# --- autologon, which the installer deliberately does not set ----------------
# Reported as UNKNOWN when absent rather than FAIL: choosing NOT to autologon is a legitimate
# security decision, and this script must not push an operator toward storing a recoverable
# credential on a box that holds a broker terminal.
$winlogon = "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon"
$auto = (Get-ItemProperty -Path $winlogon -Name AutoAdminLogon -ErrorAction SilentlyContinue).AutoAdminLogon
if ($auto -eq "1") {
    Report "autologon" "OK" "enabled -- a reboot will reach a desktop for MT5"
} else {
    Report "autologon" "UNKNOWN" ("not enabled. A REBOOT WILL NOT RESTART THE DESK: MT5 needs " +
                                  "an interactive desktop. Either set it (Autologon64.exe) or " +
                                  "accept a manual login after reboot -- but decide, do not assume")
}

# --- the supervisor heartbeat -----------------------------------------------
$hb = Join-Path $DeskRoot "logs\supervisor_heartbeat.json"
if (-not (Test-Path $hb)) {
    Report "desk heartbeat" "UNKNOWN" "no heartbeat file -- the supervisor has never run"
} else {
    try {
        $h = Get-Content $hb -Raw | ConvertFrom-Json
        $age = [int]((Get-Date).ToUniversalTime() - [datetime]::Parse($h.updated_utc).ToUniversalTime()).TotalMinutes
        switch ($h.state) {
            "RUNNING"    { Report "desk heartbeat" "OK" ("RUNNING, written ${age}m ago (pid $($h.pid)). " +
                                                          "logs\desk_output.log stays empty until this run exits -- " +
                                                          "tail logs\_desk_stdout.tmp to see it live") }
            "RESTARTING" { Report "desk heartbeat" "RESTARTING" ("$($h.detail) -- failures=$($h.consecutive_failures). " +
                                                                  "See logs\desk_output.log for what run_desk.py printed") }
            "GAVE_UP"    { Report "desk heartbeat" "GAVE_UP" "$($h.detail). See logs\desk_output.log for why" }
            default      { Report "desk heartbeat" "UNKNOWN" "state=$($h.state) written ${age}m ago" }
        }
    } catch {
        Report "desk heartbeat" "UNREADABLE" "heartbeat exists but will not parse: $_"
    }
}

# --- is a desk process actually up ------------------------------------------
$procs = @(Get-CimInstance Win32_Process -Filter "Name like '%python%'" -ErrorAction SilentlyContinue |
           Where-Object { $_.CommandLine -like "*run_desk.py*" })
if ($procs.Count -gt 0) {
    Report "desk process" "OK" "$($procs.Count) run_desk.py process(es): pid $($procs.ProcessId -join ', ')"
} else {
    Report "desk process" "NOT RUNNING" "no python process with run_desk.py in its command line"
}

# --- the evidence trail, which is the only proof of work --------------------
# A ledger that exists but has not grown is the failure that looks most like success: process
# up, no errors, nothing being decided.
$ledger = Join-Path $DeskRoot "state\ledger.jsonl"
if (-not (Test-Path $ledger)) {
    Report "ledger" "UNKNOWN" "state\ledger.jsonl does not exist yet"
} else {
    $li = Get-Item $ledger
    $mins = [int]((Get-Date) - $li.LastWriteTime).TotalMinutes
    $rows = (Get-Content $ledger -ReadCount 0 | Measure-Object -Line).Lines
    if ($mins -le 120) {
        Report "ledger" "OK" "$rows rows, last written ${mins}m ago"
    } else {
        Report "ledger" "STALE" ("$rows rows but last written ${mins}m ago -- the desk is not " +
                                 "journalling. A live process with a stale ledger is the failure " +
                                 "that looks most like a quiet market")
    }
}

Write-Host ""
if ($fails -eq 0) {
    Write-Host "no failures. Note any UNKNOWN above -- those are unmeasured, not fine."
} else {
    Write-Host "$fails check(s) FAILED. The desk is not in the state you think it is."
}
Write-Host ""
Write-Host "The only test that proves reboot survival: reboot, touch nothing, wait for Telegram."
exit ([Math]::Min($fails, 1))

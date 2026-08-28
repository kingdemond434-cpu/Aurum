<#
.SYNOPSIS
    Pull the desk's branch, prove it works, and restart only when it is safe.

.DESCRIPTION
    WHY THIS EXISTS

    Every change to this desk required the operator to open a terminal, pull,
    re-run the installer and restart. That is not a small friction: it means a
    fix sits unused for as long as the operator is away from the box, and on
    2026-08-27 a set of fixes sat undeployed for hours while the desk kept
    reproducing the exact defects they addressed.

    There is no technical reason for it. The box already has git, already runs
    scheduled tasks, and already pushes on the quant side. Nobody had written
    this file.

    WHAT MAKES AUTO-UPDATE SAFE, AND WHAT WOULD MAKE IT DANGEROUS

    A blind `git pull; restart` loop is worse than manual updates. It can pull a
    broken commit and restart into a crash loop while nobody is watching, and it
    can restart in the middle of a live position. Both are addressed and neither
    is addressed by hoping:

      TESTS RUN BEFORE THE SWAP, NOT AFTER. The suite runs against the NEW code
      while the OLD desk is still running. If it fails, the working tree is
      rolled back to the commit that was running and the desk is never touched.
      A red suite means no restart, not a restart and an apology.

      IT WILL NOT RESTART ON AN OPEN POSITION unless -Force is passed. The
      checkpoint does survive a restart -- rehydrate() restores the position and
      now the full excursion record too -- but a restart mid-trade still costs
      tick continuity, and there is no reason to spend that when the update can
      wait for flat. A desk that is flat is idle by definition.

      IT DOES NOTHING WHEN THERE IS NOTHING TO DO. No new commits means exit 0
      in under a second, which is what almost every run will be.

    WHAT IT WILL NOT DO

    It will not re-run the installer. Registering scheduled tasks changes the
    machine's configuration, can prompt, and can fail in ways that leave the
    desk unregistered -- that stays a deliberate operator act. When a commit
    changes Install-AurumStartup.ps1 this script SAYS SO and asks for a hand
    rather than doing it quietly.

.PARAMETER DeskRoot
    The Aurum checkout. Defaults to this script's grandparent.

.PARAMETER Branch
    Branch to track. Defaults to the current one.

.PARAMETER Force
    Restart even with a position open.

.PARAMETER SkipTests
    Update without proving the new code works. Do not use this on a schedule.
#>
[CmdletBinding()]
param(
    [string] $DeskRoot,
    [string] $Branch,
    [switch] $Force,
    [switch] $SkipTests
)

$ErrorActionPreference = "Stop"
if (-not $DeskRoot) { $DeskRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot) }
Set-Location $DeskRoot

$logDir = Join-Path $DeskRoot "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir "update.log"
function Say($m) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $m"
    Write-Host $line
    Add-Content -Path $log -Value $line -Encoding utf8
}

# RESOLVE GIT EXPLICITLY, AND SAY SO IF IT IS NOT THERE.
#
# A scheduled task runs with a minimal environment: git is frequently on the
# interactive user's PATH and not on the task's. With ErrorActionPreference
# "Stop", the first `git` call then threw before Say() had ever been reached, so
# the task exited 1 and wrote NO LOG AT ALL. Observed on the live box: the
# watchdog reported "firing and FAILING" and logs\update.log did not exist, which
# is the least diagnosable failure this script could possibly produce.
$git = (Get-Command git -ErrorAction SilentlyContinue).Source
if (-not $git) {
    foreach ($c in @("$env:ProgramFiles\Git\cmd\git.exe",
                     "${env:ProgramFiles(x86)}\Git\cmd\git.exe",
                     "$env:LOCALAPPDATA\Programs\Git\cmd\git.exe")) {
        if (Test-Path $c) { $git = $c; break }
    }
}
if (-not $git) {
    Say "ABORT: git is not on this task's PATH and was not found in the usual"
    Say "       install locations. A scheduled task does not inherit the"
    Say "       interactive PATH; pass the full path or add git to the machine"
    Say "       PATH. Nothing was changed."
    exit 1
}
Set-Alias -Name git -Value $git -Scope Script

try {

if (-not $Branch) { $Branch = (git rev-parse --abbrev-ref HEAD).Trim() }
$before = (git rev-parse HEAD).Trim()

# NEVER discard local work. A dirty tree on the desk box is far more likely to
# be someone mid-investigation than junk, and a script that resolves that by
# throwing it away is a script that eventually throws away the one thing that
# mattered. Stop and say so.
$dirty = (git status --porcelain) | Where-Object { $_ }
if ($dirty) {
    Say "SKIP: working tree is dirty ($($dirty.Count) file(s)). Not touching it."
    exit 0
}

git fetch origin $Branch 2>&1 | Out-Null
$after = (git rev-parse "origin/$Branch").Trim()
if ($before -eq $after) {
    # A LINE EVEN WHEN THERE IS NOTHING TO DO. Silence here is the same silence
    # as a crash, and the whole point of the log is telling them apart. Written
    # to a separate file so the main log stays a record of CHANGES and does not
    # fill with 48 no-ops a day.
    Set-Content -Path (Join-Path $logDir "update_lastcheck.txt") -Encoding utf8 `
        -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  up to date at $($before.Substring(0,7))"
    exit 0
}

Say "new commits on ${Branch}: $($before.Substring(0,7)) -> $($after.Substring(0,7))"
git merge --ff-only "origin/$Branch" 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Say "ABORT: not a fast-forward. The box has diverged from the remote and"
    Say "       resolving that automatically could discard either side."
    exit 1
}

# TESTS AGAINST THE NEW CODE, WHILE THE OLD DESK IS STILL RUNNING.
$py = Join-Path $DeskRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "py" }
if (-not $SkipTests) {
    Say "running the suite against the new code (old desk still live)..."
    # FORCE UTF-8 ON THE CHILD. Windows Python defaults to the LOCALE encoding
    # (cp1252 here), and this codebase's prose is full of em-dashes -- every
    # docstring, every test name, every refusal reason. Under cp1252 a test that
    # merely PRINTS one raises UnicodeEncodeError, and a source file read with
    # no explicit encoding raises UnicodeDecodeError. Neither is a real defect,
    # and both arrive here as "TESTS FAILED", roll the box back, and exit 1 --
    # a red suite caused by the alphabet, blocking every future deployment while
    # each log line says the update ran.
    #
    # The source-side half (an explicit encoding= on 93 read_text/write_text/
    # open calls) is in the same commit. This is the OUTPUT side, which no
    # amount of source hygiene reaches. Set on this process so the child
    # inherits it; the desk's own environment is untouched.
    $env:PYTHONUTF8 = "1"
    $env:PYTHONIOENCODING = "utf-8"
    & $py -m pytest -q 2>&1 | Select-Object -Last 3 | ForEach-Object { Say "  $_" }
    if ($LASTEXITCODE -ne 0) {
        Say "TESTS FAILED — rolling back to $($before.Substring(0,7)). Desk untouched."
        git reset --hard $before 2>&1 | Out-Null
        exit 1
    }
    Say "suite green"
}

# The installer is the operator's to run: registering tasks changes machine
# configuration and can fail leaving the desk unregistered.
$changed = git diff --name-only $before $after
if ($changed -match "Install-AurumStartup\.ps1") {
    Say "NOTE: this update changes Install-AurumStartup.ps1 — scheduled tasks may"
    Say "      need re-registering BY HAND. Not done automatically on purpose."
}

# Do not interrupt a live position without being told to.
$statePath = Join-Path $DeskRoot "state\service_state.json"
if ((Test-Path $statePath) -and -not $Force) {
    try {
        $st = Get-Content $statePath -Raw | ConvertFrom-Json
        if ($st.open_trade) {
            Say "code updated; restart DEFERRED — a position is open. The next run"
            Say "  picks it up once flat, or pass -Force."
            exit 0
        }
    } catch { Say "could not read $statePath ($($_.Exception.Message)) — restarting anyway" }
}

Say "restarting AurumSignalDesk"
schtasks /End /TN "AurumSignalDesk" 2>&1 | Out-Null
Start-Sleep -Seconds 3
schtasks /Run /TN "AurumSignalDesk" 2>&1 | Out-Null
Say "updated to $($after.Substring(0,7)) and restarted"

} catch {
    # NOTHING MAY FAIL SILENTLY HERE. An updater that dies without a line is
    # indistinguishable from one that ran and found nothing, and the watchdog
    # can only report "exited 1" with no way to say why.
    Say "UNHANDLED: $($_.Exception.GetType().Name): $($_.Exception.Message)"
    Say "  at $($_.InvocationInfo.ScriptLineNumber): $($_.InvocationInfo.Line.Trim())"
    exit 1
}

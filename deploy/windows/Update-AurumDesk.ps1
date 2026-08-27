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
if ($before -eq $after) { exit 0 }          # the common case: nothing to do

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

<#
.SYNOPSIS
    Move the quant desk's exported findings into Aurum's absorption inbox.

.DESCRIPTION
    WHY THIS IS A SEPARATE STEP AND NOT AN IMPORT

    The two desks are separate repositories with separate lifecycles. A Python
    step on either side that reached into the other's checkout would fail in a
    way neither desk owns -- Aurum's `step_absorb` says exactly this from its
    side, and the quant exporter says it from the other. The transport is
    therefore operator-owned, and this file is that step written down so it is
    not re-invented differently every time.

    WHAT IT MOVES

      <quant>\desks\mt5\reports\aurum_findings.jsonl     (written daily by
                                                          research\daily_cycle.py)
        ->  <aurum>\inbox\quant_findings.jsonl           (read daily by
                                                          aurum_cycle.py step_absorb)

    APPEND, NEVER OVERWRITE, AND DEDUPED ON THE WAY IN

    Overwriting silently drops any finding that was exported, absorbed, and
    then rotated out of the source. Appending alone grows the inbox without
    bound. So this appends only rows whose (statement, measured_on) pair is not
    already present -- the SAME pair the quant exporter dedups on and the same
    one Aurum's Absorber content-hashes on, so all three agree about what
    counts as the same claim. Running it twice is a no-op.

    ABSORPTION IS NOT ADOPTION. Every row enters as a SEALED HYPOTHESIS at zero
    authority. A finding measured on CADJPY is evidence about CADJPY; asserting
    it about XAUUSD because the same code produced it is precisely the
    cargo-culting the absorber exists to refuse.

.PARAMETER QuantRoot
    The quant checkout, e.g. C:\quant.

.PARAMETER AurumRoot
    The Aurum checkout. Defaults to this script's grandparent.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File deploy\windows\Sync-QuantFindings.ps1 -QuantRoot C:\quant
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string] $QuantRoot,
    [string] $AurumRoot
)

$ErrorActionPreference = "Stop"

if (-not $AurumRoot) {
    $d = $PSScriptRoot
    if (-not $d) { $d = Split-Path -Parent $MyInvocation.MyCommand.Path }
    if (-not $d) { throw "cannot locate this script; pass -AurumRoot explicitly" }
    $AurumRoot = Split-Path -Parent (Split-Path -Parent $d)
}

$src = Join-Path $QuantRoot "desks\mt5\reports\aurum_findings.jsonl"
$dstDir = Join-Path $AurumRoot "inbox"
$dst = Join-Path $dstDir "quant_findings.jsonl"

$stamp = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")

if (-not (Test-Path $src)) {
    # NOT an error, and deliberately not silent. reports\ is gitignored and
    # lives on whichever host ran the hunts, so its absence here is a real and
    # expected state -- but it must be SAID, because "no findings arrived" and
    # "the source file was never here" are different facts and only one of them
    # means the quant desk learned nothing.
    Write-Host "$stamp no source at $src -- nothing to sync."
    Write-Host "         That is UNMEASURED, not 'no new findings'."
    # EXIT 3, NOT 0. Everything above prints to stdout, and Task Scheduler
    # records only the EXIT CODE -- so exiting 0 here meant the task reported
    # SUCCESS while nothing had been delivered for 178 hours, and task_health
    # dutifully passed it. "Ran fine" and "ran fine and delivered nothing" are
    # different facts; a transport that cannot tell them apart at the exit code
    # is a transport nobody can monitor.
    #
    # 3 is this script's own vocabulary for "ran, source absent, delivered
    # nothing", listed in task_health.BENIGN_PER_TASK so it is reported as the
    # UNMEASURED state it is rather than as either a pass or a crash. Same fix
    # as the quant desk's sync_shadow_to_git.ps1, which had the identical bug.
    exit 3
}

New-Item -ItemType Directory -Force -Path $dstDir | Out-Null
if (-not (Test-Path $dst)) { New-Item -ItemType File -Path $dst | Out-Null }

function Read-Rows($path) {
    $out = @()
    foreach ($line in (Get-Content -LiteralPath $path -Encoding utf8)) {
        if (-not $line.Trim()) { continue }
        try { $out += ($line | ConvertFrom-Json) }
        catch {
            # Reported, never dropped silently: a transport that quietly
            # discards what it cannot parse is how a finding goes missing with
            # every step still reporting success.
            Write-Host ("  WARNING malformed row skipped in {0}: {1}" -f
                        (Split-Path -Leaf $path), $line.Substring(0, [Math]::Min(90, $line.Length)))
        }
    }
    return $out
}

function Get-Key($r) {
    "{0}||{1}" -f ("$($r.statement)").Trim().ToLower(), ("$($r.measured_on)").Trim().ToLower()
}

$have = @{}
foreach ($r in (Read-Rows $dst)) { $have[(Get-Key $r)] = $true }

$new = @()
foreach ($line in (Get-Content -LiteralPath $src -Encoding utf8)) {
    if (-not $line.Trim()) { continue }
    try { $r = $line | ConvertFrom-Json } catch { continue }
    if (-not $have.ContainsKey((Get-Key $r))) {
        $new += $line
        $have[(Get-Key $r)] = $true      # guards duplicates WITHIN one source file
    }
}

if ($new.Count -eq 0) {
    Write-Host ("  0 new finding(s); inbox already holds {0}. Steady state for a daily run." -f $have.Count)
} else {
    Add-Content -LiteralPath $dst -Value $new -Encoding utf8
    Write-Host ("  {0} new finding(s) appended to {1}" -f $new.Count, $dst)
    foreach ($line in $new) {
        try {
            $r = $line | ConvertFrom-Json
            $s = "$($r.statement)"
            Write-Host ("    [{0}] {1}..." -f $r.grade, $s.Substring(0, [Math]::Min(88, $s.Length)))
        } catch { }
    }
}

Write-Host "$stamp sync complete. Aurum's next cycle queues anything new as a"
Write-Host "         SEALED HYPOTHESIS at zero authority -- absorption is not adoption."

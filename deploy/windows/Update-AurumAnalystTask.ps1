<#
.SYNOPSIS
    Update only the live desk task's analyst launch action.

.DESCRIPTION
    This intentionally preserves the existing task principal, interactive logon trigger and
    settings. It is safe to run over OpenSSH, where USERDOMAIN/USERNAME can differ from the
    interactive desktop identity required by MT5 and re-registering the whole task would fail
    Windows account-to-SID mapping.
#>
[CmdletBinding()]
param(
    [string] $DeskRoot = "C:\Aurum",
    [string] $TaskName = "AurumSignalDesk",
    [switch] $GptPrimary
)

$ErrorActionPreference = "Stop"
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
$supervisor = Join-Path $DeskRoot "deploy\windows\Start-AurumDesk.ps1"
if (-not (Test-Path $supervisor)) { throw "supervisor not found at $supervisor" }

$deskArgs = @("--shadow")
if ($GptPrimary) {
    # Temporary operating mode for a known Claude subscription cooldown. Do
    # not probe a provider that has already reported its reset time on every
    # bar; route directly to the healthy subscription instead.
    $deskArgs += @("--provider", "codex:gpt-5.6-sol",
                   "--fallback-provider", "none")
} else {
    $deskArgs += @("--provider", "claudecode:claude-opus-5",
                   "--fallback-provider", "codex:gpt-5.6-sol")
}
$deskArgs += @("--expect-broker", "Fusion", "--wake-every-bar",
               "--universe", "--effort", "high",
               "--timeframes", "M5")
$joined = $deskArgs -join "|"
$arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden " +
             "-File `"$supervisor`" -DeskRoot `"$DeskRoot`" -DeskArgsJoined `"$joined`""
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arguments `
                                  -WorkingDirectory $DeskRoot

# Supplying only -Action changes only the action. In particular, do not recreate the principal:
# MT5 must retain the already-proven Administrator/Interactive desktop identity.
Set-ScheduledTask -TaskName $TaskName -Action $action | Out-Null

$installed = Get-ScheduledTask -TaskName $TaskName
$actual = $installed.Actions[0].Arguments
if ($GptPrimary) {
    if ($actual -notmatch [regex]::Escape("--provider|codex:gpt-5.6-sol")) {
        throw "task action did not retain GPT as primary"
    }
} elseif ($actual -notmatch [regex]::Escape("--fallback-provider|codex:gpt-5.6-sol")) {
    throw "task action did not retain the GPT fallback"
}
if ($actual -match [regex]::Escape("--numeric-only")) {
    throw "task action still disables the GPT chart pack"
}

Write-Host "UPDATED: $TaskName"
Write-Host "  principal : $($installed.Principal.UserId) / $($installed.Principal.LogonType)"
Write-Host ("  analyst   : " + $(if ($GptPrimary) {
    "ChatGPT subscription primary (gpt-5.6-sol, high)"
} else {
    "Claude subscription -> ChatGPT subscription (gpt-5.6-sol, high)"
}))
Write-Host "  charts    : enabled for GPT failover"
Write-Host "  cadence   : every M5 close; each packet sees live M1/M5/M15/H1/H4 charts"
Write-Host "  mode      : shadow (no order placement)"

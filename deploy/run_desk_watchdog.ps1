param(
    [switch]$Live
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repo ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    $python = (Get-Command python -ErrorAction Stop).Source
}
$runner = Join-Path $repo "run_desk.py"
$logDir = Join-Path $repo "logs"
$stopFile = Join-Path $repo "state\STOP_WATCHDOG"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

$mode = if ($Live) { "--live" } else { "--shadow" }
$deskArgs = @(
    $runner, $mode,
    "--feed", "mt5",
    "--provider", "claudecode:claude-opus-5",
    "--fallback-provider", "codex:gpt-5.6-sol",
    "--expect-broker", "Vantage",
    "--management", "heuristic",
    "--shadow-management",
    "--open-poll-seconds", "1",
    "--flat-poll-seconds", "15",
    "--closed-poll-seconds", "60"
)

Push-Location $repo
try {
    & $python $runner --preflight --feed mt5 `
        --provider claudecode:claude-opus-5 `
        --fallback-provider codex:gpt-5.6-sol `
        --expect-broker Vantage
    if ($LASTEXITCODE -ne 0) {
        throw "Aurum preflight failed; watchdog was not started"
    }

    while (-not (Test-Path -LiteralPath $stopFile)) {
        $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
        Add-Content -LiteralPath (Join-Path $logDir "watchdog.log") `
            -Value "$stamp starting Aurum ($mode, charts enabled)"
        & $python @deskArgs *>> (Join-Path $logDir "desk.log")
        $code = $LASTEXITCODE
        $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
        Add-Content -LiteralPath (Join-Path $logDir "watchdog.log") `
            -Value "$stamp Aurum exited code $code; restarting in 10 seconds"
        if (-not (Test-Path -LiteralPath $stopFile)) {
            Start-Sleep -Seconds 10
        }
    }
}
finally {
    Pop-Location
}

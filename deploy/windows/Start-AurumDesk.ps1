<#
.SYNOPSIS
    Supervisor for the Aurum signal desk on Windows. Keeps it running; says when it cannot.

.DESCRIPTION
    THE JOB THIS DOES THAT A STARTUP SHORTCUT DOES NOT

    Putting `run_desk.py` in the Startup folder starts it once. A desk that dies at 04:00 --
    MT5 dropped the terminal, the network blipped, the CLI hit a quota wall -- then stays dead
    until somebody notices, and the failure looks exactly like a quiet market. The whole reason
    this desk exists is to be watching when you are not.

    So the desk runs under a supervisor loop with backoff, and the supervisor writes a HEARTBEAT
    file on every iteration. That file is what makes "running", "restarting" and "gave up"
    distinguishable from outside the process, which is the distinction that matters at 3am and
    the one a bare shortcut cannot express.

    WHY BACKOFF AND WHY A CEILING

    A desk that crashes instantly on a config error would, without backoff, respawn thousands of
    times a minute, fill the disk with logs and hammer the broker's endpoint. With unlimited
    backoff it would instead retry every few hours forever, which is indistinguishable from
    working. Neither is honest. It restarts with growing delay up to a cap, and after
    MaxConsecutiveFailures fast failures it STOPS and records why -- a supervisor that cannot
    keep the thing alive should say so rather than hide it in a retry loop.

    A "fast failure" is one where the desk died sooner than HealthySeconds. A desk that ran for
    six hours and then died is a different event from one that dies in four seconds, and only the
    second means the configuration is broken; the counter resets on the first.

.PARAMETER DeskRoot
    The Aurum checkout. Defaults to this script's own grandparent (deploy\windows\..\..), which
    is correct by construction wherever the repo is cloned and needs no configuration.

.PARAMETER DeskArgs
    Arguments passed to run_desk.py. The default matches the verified-working VPS launch:
    shadow mode, the subscription analyst, numeric-only (mandatory with claudecode -- the CLI
    takes no image input), and the broker assertion.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File deploy\windows\Start-AurumDesk.ps1
#>
[CmdletBinding()]
param(
    [string]   $DeskRoot,
    [string[]] $DeskArgs = @("--shadow", "--provider", "claudecode:claude-opus-5",
                             "--numeric-only", "--expect-broker", "Fusion"),
    [int]      $HealthySeconds = 300,
    [int]      $MaxConsecutiveFailures = 8,
    [int]      $MaxBackoffSeconds = 300
)

$ErrorActionPreference = "Stop"

# See Install-AurumStartup.ps1 for why this is resolved here rather than as a param default:
# $PSScriptRoot is empty when used inside a param() default on Windows PowerShell 5.1.
if (-not $DeskRoot) {
    $scriptDir = $PSScriptRoot
    if (-not $scriptDir) { $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path }
    if (-not $scriptDir) { $scriptDir = Split-Path -Parent $PSCommandPath }
    if (-not $scriptDir) {
        throw "cannot determine this script's own location to derive -DeskRoot; pass it explicitly"
    }
    $DeskRoot = Split-Path -Parent (Split-Path -Parent $scriptDir)
}

$logDir    = Join-Path $DeskRoot "logs"
$log       = Join-Path $logDir "supervisor.log"
$heartbeat = Join-Path $logDir "supervisor_heartbeat.json"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

function Write-Log($msg) {
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Write-Host $line
    Add-Content -Path $log -Value $line -Encoding utf8
}

function Write-Heartbeat($state, $detail, $failures) {
    # Written on EVERY state change, so an outside reader can tell a healthy desk from a
    # crashloop from a supervisor that has given up -- without attaching to the process.
    @{
        state          = $state          # STARTING | RUNNING | RESTARTING | GAVE_UP
        detail         = $detail
        consecutive_failures = $failures
        pid            = $PID
        updated_utc    = (Get-Date).ToUniversalTime().ToString("o")
    } | ConvertTo-Json | Set-Content -Path $heartbeat -Encoding utf8
}

# --- resolve the interpreter -------------------------------------------------
# `python3` DOES NOT EXIST ON WINDOWS. It is the Linux name, and calling it is the single most
# common way these instructions fail on a fresh box. Resolution order: the venv this repo may
# carry, then the `py` launcher (which Windows installs and which survives PATH damage), then a
# bare `python`. Guessing wrong here produces "not recognized as the name of a cmdlet", which
# reads like the SCRIPT is missing rather than the interpreter.
function Resolve-Python {
    $venv = Join-Path $DeskRoot ".venv\Scripts\python.exe"
    if (Test-Path $venv) { return @{ Exe = $venv; Args = @() } }
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) { return @{ Exe = $py.Source; Args = @("-3") } }
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) { return @{ Exe = $python.Source; Args = @() } }
    return $null
}

$interp = Resolve-Python
if (-not $interp) {
    $msg = "NO PYTHON FOUND. Looked for .venv\Scripts\python.exe, the 'py' launcher, and 'python' on PATH."
    Write-Log "FATAL: $msg"
    Write-Heartbeat "GAVE_UP" $msg 0
    exit 1
}

$deskScript = Join-Path $DeskRoot "run_desk.py"
if (-not (Test-Path $deskScript)) {
    $msg = "run_desk.py not found under $DeskRoot -- wrong -DeskRoot, or an incomplete clone."
    Write-Log "FATAL: $msg"
    Write-Heartbeat "GAVE_UP" $msg 0
    exit 1
}

Write-Log "supervisor starting: $($interp.Exe) $deskScript $($DeskArgs -join ' ')"
Write-Log "desk root: $DeskRoot"
Write-Heartbeat "STARTING" "supervisor launched" 0

$failures = 0
while ($true) {
    $startedAt = Get-Date
    Write-Heartbeat "RUNNING" "desk started $($startedAt.ToUniversalTime().ToString('o'))" $failures

    $argv = @()
    $argv += $interp.Args
    $argv += $deskScript
    $argv += $DeskArgs

    # Run in the FOREGROUND and inherit stdout/stderr: run_desk.py already journals every
    # decision to state/ledger.jsonl, so the supervisor's log is for the process lifecycle only
    # and must not duplicate the desk's own evidence trail.
    & $interp.Exe @argv
    $code = $LASTEXITCODE
    $ranFor = [int]((Get-Date) - $startedAt).TotalSeconds

    if ($code -eq 0) {
        Write-Log "desk exited cleanly (0) after ${ranFor}s -- not restarting"
        Write-Heartbeat "GAVE_UP" "desk exited 0 after ${ranFor}s (deliberate stop)" $failures
        exit 0
    }

    if ($ranFor -ge $HealthySeconds) {
        # It was alive long enough to have been working. Whatever killed it is an incident, not
        # a broken configuration, so the failure budget resets.
        Write-Log "desk exited $code after ${ranFor}s (was healthy) -- restarting, counter reset"
        $failures = 0
        $delay = 10
    } else {
        $failures++
        $delay = [Math]::Min([int][Math]::Pow(2, $failures) * 5, $MaxBackoffSeconds)
        Write-Log "desk exited $code after only ${ranFor}s -- fast failure $failures/$MaxConsecutiveFailures"
    }

    if ($failures -ge $MaxConsecutiveFailures) {
        $msg = "$failures consecutive fast failures; last exit code $code. STOPPING. " +
               "Run the desk by hand to see the error: $($interp.Exe) run_desk.py --preflight"
        Write-Log "FATAL: $msg"
        Write-Heartbeat "GAVE_UP" $msg $failures
        exit 1
    }

    Write-Heartbeat "RESTARTING" "exit $code after ${ranFor}s; retrying in ${delay}s" $failures
    Write-Log "restarting in ${delay}s"
    Start-Sleep -Seconds $delay
}

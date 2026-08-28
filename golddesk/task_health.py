"""Who watches the watchdogs.

THE GAP THIS CLOSES

Every check this desk runs lives INSIDE a scheduled task. If that task is
disabled, deleted, or has been failing on every run, nothing notices -- the
checks simply stop, and stopped checks look exactly like passing ones. A watchdog
that cannot detect its own absence is a watchdog you cannot rely on, and the
failure is silent by construction.

Worse, they fail TOGETHER. Every task registers at LogonType Interactive, so a
reboot that does not reach a desktop takes the desk AND every watchdog at once.
That specific case cannot be caught from inside the box at all -- nothing is
running to catch it -- and it is named here so the limit is written down rather
than assumed away.

WHAT IS FIXED AND WHAT IS NOT

  DISABLED   re-enabled automatically. Flipping a flag on an existing
             registration is deterministic, bounded and reversible.
  MISSING    escalated. Registering a task changes machine configuration, can
             prompt, and can fail leaving the desk in a worse state than it
             started -- that stays the operator's act, as it is in the updater.
  FAILING    escalated. A task that runs and exits non-zero every time has a
             cause no restart addresses.
  STALE      escalated. A task that has not run in several intervals is either
             blocked on something or the scheduler is not firing it.

The reader is INJECTED so this module has no Windows dependency and every branch
is exercised on a Linux test box.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional, Sequence

TASK_HEALTH_VERSION = "tasks-2026-08-28-a"

#: name -> (expected interval, what its absence costs). The interval is used to
#: decide staleness; a task is stale at several times its own period, never at
#: one, because a machine can be busy.
EXPECTED: dict[str, tuple[timedelta, str]] = {
    "AurumSignalDesk":            (timedelta(days=1),      "the desk itself"),
    "AurumSignalDesk-Watchdog":   (timedelta(minutes=5),   "restarts a dead desk"),
    "AurumSignalDesk-SelfHeal":   (timedelta(minutes=15),  "every wiring and capture check"),
    "AurumSignalDesk-Update":     (timedelta(minutes=30),  "pulls and deploys fixes"),
    "AurumSignalDesk-VantageSpread": (timedelta(minutes=20), "execution-venue spread"),
    "AurumSignalDesk-Cycle":      (timedelta(days=1),      "the whole learning loop"),
    # THE QUANT DESK RUNS ON THE SAME BOX AND NOTHING WATCHED IT.
    #
    # Aurum's absorption cannot exceed what quant certifies, so a quant task
    # that stops firing degrades THIS desk -- silently, because the only symptom
    # is findings that stop arriving, and "0 new findings" is indistinguishable
    # from a quant desk that found nothing.
    #
    # Only the tasks Aurum actually depends on. Watching all seventeen would put
    # this desk in the business of policing another one, and a watchdog that
    # reports faults its owner cannot act on is noise.
    "MT5-ShadowSync":  (timedelta(minutes=15),
                        "publishes shadow_health.json -- the ONLY window this "
                        "desk has into whether quant is accruing evidence"),
    "MT5-Shadow":      (timedelta(minutes=15), "quant's forward-evidence run"),
    "MT5-QQuantGatesCertify": (timedelta(days=1),
                              "certifies survivors; the source of everything "
                              "Aurum absorbs"),
    "Aurum-Sync":      (timedelta(days=1),
                        "carries quant's findings into this desk's inbox"),
}

#: Multiples of a task's own interval before it is called stale. Three, so an
#: ordinary busy patch does not fire it and a genuinely stopped task does.
STALE_MULTIPLE = 3

#: Scheduler results that are NOT failures. These are Windows status codes, not
#: program exit codes, and reading them as errors is how a watchdog cries wolf:
#:
#:   267009  0x00041301  the task is currently RUNNING
#:   267011  0x00041303  the task has NOT YET RUN -- the normal state of a daily
#:                       task registered an hour ago, and reported as a hard
#:                       failure on the live box the first night this shipped
#:   267012  0x00041304  no more runs are scheduled
#:   267014  0x00041306  the last run was terminated by the user
BENIGN_RESULTS = frozenset({0, 267009, 267011, 267012, 267014})

#: Per-task exit codes that mean "ran fine, nothing to do". A task's own
#: vocabulary, kept here rather than in the generic set so one script's
#: convention cannot silently excuse another's real failure.
BENIGN_PER_TASK: dict[str, frozenset] = {
    # 3 = sampled the venue, archive still too thin to write a profile. That is
    # the expected state for the first hours and a successful run.
    "AurumSignalDesk-VantageSpread": frozenset({3}),
}


@dataclass(frozen=True)
class TaskInfo:
    name: str
    exists: bool
    enabled: bool
    last_run: Optional[datetime]
    last_result: Optional[int]


@dataclass(frozen=True)
class Finding:
    check: str
    ok: bool
    detail: str
    fixable: bool = False

    @property
    def line(self) -> str:
        return f"  [{'PASS' if self.ok else 'BROKEN'}] {self.check:<32} {self.detail}"


def audit(read: Callable[[str], TaskInfo],
          now: Optional[datetime] = None,
          expected: Optional[dict] = None) -> list[Finding]:
    """One finding per expected task. `read` never raises for this to work; a
    reader that throws is treated as UNMEASURED for that task rather than as a
    passing one."""
    now = now or datetime.now(timezone.utc)
    out: list[Finding] = []
    for name, (interval, why) in (expected or EXPECTED).items():
        try:
            info = read(name)
        except Exception as e:                         # noqa: BLE001
            out.append(Finding(name, True,
                               f"UNMEASURED — could not read the task ({e}). "
                               f"Not the same as healthy."))
            continue

        if not info.exists:
            out.append(Finding(name, False,
                               f"NOT REGISTERED — {why} is not running at all. "
                               f"Re-run Install-AurumStartup.ps1; registering a "
                               f"task is not done automatically."))
            continue
        if not info.enabled:
            out.append(Finding(name, False,
                               f"DISABLED — {why} is registered and switched "
                               f"off.", fixable=True))
            continue
        benign = BENIGN_RESULTS | BENIGN_PER_TASK.get(name, frozenset())
        if info.last_result is not None and info.last_result not in benign:
            out.append(Finding(name, False,
                               f"last run exited {info.last_result} — {why} is "
                               f"firing and FAILING, which no restart fixes."))
            continue
        if info.last_result == 267011:
            out.append(Finding(name, True,
                               f"registered and enabled, HAS NOT RUN YET — normal "
                               f"for a task whose first fire is still ahead"))
            continue
        if info.last_run is not None:
            age = now - info.last_run
            if age > interval * STALE_MULTIPLE:
                out.append(Finding(name, False,
                                   f"last ran {age.total_seconds() / 3600:.1f}h "
                                   f"ago, {STALE_MULTIPLE}x its {interval} "
                                   f"interval — {why} has stopped firing."))
                continue
        out.append(Finding(name, True, f"enabled, last result "
                                       f"{info.last_result if info.last_result is not None else 'n/a'}"))
    return out


def render(findings: Sequence[Finding]) -> str:
    bad = [f for f in findings if not f.ok]
    # UNMEASURED IS NOT A PASS, and the header is where that lie would live.
    # Every finding coming back unreadable -- no schtasks, a permissions problem,
    # the wrong OS -- printed "every watchdog is running", which is absence read
    # as a clean answer about the one thing that is supposed to notice absence.
    unknown = [f for f in findings if f.ok and "UNMEASURED" in f.detail]
    if findings and len(unknown) == len(findings):
        head = (f"TASK HEALTH ({TASK_HEALTH_VERSION}) — NOTHING COULD BE READ. "
                f"Whether any watchdog is running is UNKNOWN, which is not the "
                f"same as fine.")
    elif bad:
        head = f"TASK HEALTH ({TASK_HEALTH_VERSION}) — {len(bad)} WATCHDOG FAULT(S)"
    elif unknown:
        head = (f"TASK HEALTH ({TASK_HEALTH_VERSION}) — "
                f"{len(findings) - len(unknown)} running, {len(unknown)} UNREADABLE")
    else:
        head = f"TASK HEALTH ({TASK_HEALTH_VERSION}) — every watchdog is running"
    out = [head] + [f.line for f in findings]
    if bad:
        out += ["",
                "  A stopped check looks exactly like a passing one, which is why",
                "  these are read rather than assumed. NOT COVERED, and it cannot",
                "  be from in here: every task runs at LogonType Interactive, so",
                "  a reboot that never reaches a desktop takes the desk and all",
                "  of these together and nothing is left running to notice."]
    return "\n".join(out)

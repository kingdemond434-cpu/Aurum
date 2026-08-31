"""Fix what is mechanically fixable. Escalate what is not. Never guess between.

THE OPERATOR'S QUESTION, and it is the right one: if the desk can DETECT its own
faults, why does a human have to fix them?

For a large class of faults, no reason at all, and this module fixes those
without asking. For another class the answer is that fixing means WRITING CODE,
and a process that writes and deploys its own code into a live trading desk
unattended can introduce a losing bug, widen a risk limit, or reach the ruin
rail. Those escalate loudly instead. The value is not that everything is
automatic; it is that the line between the two is EXPLICIT and allowlisted
rather than decided in the moment.

WHAT MAKES AN ACTION SAFE ENOUGH TO TAKE UNATTENDED

Four properties, and an action needs all four:

  DETERMINISTIC   one remedy, no judgement about which to apply
  BOUNDED         it changes operational state, never trading behaviour
  REVERSIBLE      the worst case is a restart, not a lost position
  RATE-LIMITED    it cannot fire in a loop; a fault that survives its own
                  remedy is a DIFFERENT fault and must escalate

THE THINGS THIS WILL NEVER DO, enumerated so a later edit has to delete a line
rather than merely add one:

  write, generate or edit code
  change a threshold, a risk limit, a stop, or a position size
  arm live trading, or touch the deadman rail
  delete a ledger, a checkpoint, or any evidence
  close, open or modify a position

Every one of those is either the principal's act or a design decision, and both
are the wrong things to do at 3am with nobody watching.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional, Sequence

log = logging.getLogger(__name__)

REMEDIATE_VERSION = "remediate-2026-08-28-a"

#: Minimum gap between two remedies for the SAME fault. A fault that survives
#: its own remedy is a different fault, and re-applying the same action faster
#: than the system can respond is how a self-healer becomes a crash loop.
COOLDOWN = timedelta(minutes=30)

#: Attempts on one fault before it stops being mechanical and starts being an
#: escalation. Three says "the obvious remedy does not work here".
MAX_ATTEMPTS = 3


@dataclass(frozen=True)
class Remedy:
    """One allowlisted action. `apply` is injected so nothing here shells out
    on its own and every call site is visible to a test."""
    fault: str
    action: str
    why: str
    apply: Callable[[], bool]


@dataclass
class Outcome:
    fault: str
    action: str
    taken: bool
    detail: str


@dataclass
class Remediator:
    """Holds the attempt history so cooldown and the attempt cap are real."""
    attempts: dict = field(default_factory=dict)      # fault -> [datetime, ...]

    def _allowed(self, fault: str, now: datetime) -> tuple[bool, str]:
        hist = [t for t in self.attempts.get(fault, []) if now - t < timedelta(days=1)]
        self.attempts[fault] = hist
        if len(hist) >= MAX_ATTEMPTS:
            return False, (f"{len(hist)} attempts in 24h — the obvious remedy does "
                           f"not work here, so this is no longer a mechanical fault")
        if hist and (now - hist[-1]) < COOLDOWN:
            mins = (COOLDOWN - (now - hist[-1])).total_seconds() / 60
            return False, f"cooling down, {mins:.0f} min left"
        return True, ""

    def run(self, remedies: Sequence[Remedy],
            now: Optional[datetime] = None) -> list[Outcome]:
        now = now or datetime.now(timezone.utc)
        out: list[Outcome] = []
        for r in remedies:
            ok, why = self._allowed(r.fault, now)
            if not ok:
                out.append(Outcome(r.fault, r.action, False, f"SKIPPED — {why}"))
                continue
            self.attempts.setdefault(r.fault, []).append(now)
            try:
                done = bool(r.apply())
            except Exception as e:                     # noqa: BLE001
                # A remedy that raises must never take the caller down with it.
                # The fault is still there and will be reported again next pass.
                out.append(Outcome(r.fault, r.action, False,
                                   f"FAILED — {type(e).__name__}: {e}"))
                continue
            out.append(Outcome(r.fault, r.action, done,
                               "applied" if done else "declined by the action"))
        return out


# --------------------------------------------------------------------------
# The allowlist: audit finding -> remedy, or nothing.
# --------------------------------------------------------------------------

def plan(findings: Sequence, *, restart_desk: Callable[[], bool],
         refresh_flows: Optional[Callable[[], bool]] = None,
         sample_spread: Optional[Callable[[], bool]] = None,
         refresh_macro: Optional[Callable[[], bool]] = None,
         rotate_logs: Optional[Callable[[], bool]] = None,
         sync_quant: Optional[Callable[[], bool]] = None,
         enable_task: Optional[Callable[[str], bool]] = None) -> tuple[list[Remedy], list]:
    """Split audit findings into what can be fixed and what must escalate.

    Returns (remedies, escalations). A finding that maps to no remedy is NOT
    silently dropped -- it escalates, because a fault nobody is told about is
    worse than one nobody can fix.
    """
    remedies: list[Remedy] = []
    escalate: list = []
    for f in findings:
        if f.ok:
            continue

        if f.check == "cohorts":
            # MECHANICAL. Cohorts are built at boot from the ledger, so a desk
            # holding none while resolved trades exist is a desk that booted
            # before they resolved. A restart genuinely fixes it -- and if it
            # does not, the attempt cap turns it into an escalation, which is
            # the correct second answer.
            remedies.append(Remedy(
                f.check, "restart the desk",
                "cohorts are rebuilt at boot from the ledger; a restart is the "
                "whole remedy when the ledger has since grown",
                restart_desk))

        elif f.check == "ledger growth":
            # MECHANICAL, with a caveat the message carries: a stalled ledger
            # over a weekend is normal. Restarting then costs nothing, and
            # restarting a genuinely wedged desk is the only remedy available
            # without reading code.
            remedies.append(Remedy(
                f.check, "restart the desk",
                "a desk that has stopped writing decisions is either wedged or "
                "the venue is shut; a restart is harmless in the second case",
                restart_desk))

        elif f.check == "checkpoint":
            # MECHANICAL. A checkpoint that stopped moving means the loop is
            # wedged; a restart is the only remedy available without reading
            # code, and it is the same one the watchdog would eventually apply.
            remedies.append(Remedy(
                f.check, "restart the desk",
                "a desk that has stopped persisting state loses everything since "
                "the last write if it crashes",
                restart_desk))

        elif f.check == "spread profile" and sample_spread is not None:
            # MECHANICAL, and it may legitimately DECLINE: the sampler refuses
            # unless the attached terminal is the execution venue. A decline is
            # recorded as not-taken rather than as a failure, because refusing
            # to measure the wrong venue is the sampler working correctly.
            remedies.append(Remedy(
                f.check, "sample the execution venue's spread",
                "the sampler builds the profile from live quotes and refuses if "
                "the attached terminal is not the execution venue",
                sample_spread))

        elif f.check == "macro" and refresh_macro is not None:
            remedies.append(Remedy(
                f.check, "refetch the macro drivers",
                "a blocked or rate-limited public feed usually clears on retry; "
                "if it does not, the attempt cap turns this into an escalation",
                refresh_macro))

        elif f.check == "disk" and rotate_logs is not None:
            # MECHANICAL and NARROW. Deletes ROTATED LOGS ONLY -- never the
            # ledger, never a checkpoint, never tick archives. A disk remedy
            # that can reach evidence is a disk remedy that eventually destroys
            # it, which is worse than a full disk.
            remedies.append(Remedy(
                f.check, "delete rotated logs",
                "rotated logs only — never the ledger, a checkpoint or an archive",
                rotate_logs))

        elif getattr(f, "fixable", False) and enable_task is not None:
            # A DISABLED scheduled task. The only task-control action taken
            # automatically: flipping a flag on an existing registration is
            # deterministic, bounded and reversible. REGISTERING one changes
            # machine configuration, can prompt, and can fail leaving the desk
            # worse off -- task_health marks only the disabled case fixable, and
            # a MISSING task escalates for exactly that reason.
            name = f.check
            remedies.append(Remedy(
                name, f"re-enable {name}",
                "a registered task that is switched off; re-enabling flips a "
                "flag and registers nothing",
                lambda n=name: enable_task(n)))

        elif f.check == "analyst answering":
            # MECHANICAL, and the desk's own restart is the remedy: a wedged CLI
            # session or a stale login is cleared by a fresh process, and that is
            # the same action the watchdog would eventually take. If it does not
            # work, the attempt cap escalates it -- which is the correct second
            # answer for an expired credential.
            remedies.append(Remedy(
                f.check, "restart the desk",
                "a wedged provider session clears on a fresh process; an expired "
                "login does not, and the attempt cap says which it was",
                restart_desk))

        elif f.check in ("calibration", "edge", "selection"):
            # NEVER MECHANICAL, and the most important refusal in this file.
            #
            # "The reads resolve negative" is not fixed by restarting anything.
            # It is fixed by changing what the analyst is asked or by stopping
            # trading the mechanism, and both are judgement made against
            # evidence. A process that responded to a bad edge by adjusting its
            # own inputs would be a desk tuning itself toward a scorecard.
            escalate.append(f)

        elif f.check == "came back after boot":
            # MECHANICAL. The desk did not restart after a reboot; starting it
            # is the whole remedy, and it is the same action the watchdog takes.
            remedies.append(Remedy(
                f.check, "restart the desk",
                "the machine came back and the desk did not; starting it is the "
                "entire fix, and the attempt cap escalates if it will not stay up",
                restart_desk))

        elif f.check in ("analyst latency", "analyst model"):
            # NOT MECHANICAL. Latency drifting into the budget is a provider
            # capacity question and a model that changed under the desk is a
            # configuration question. Restarting hides both for one cycle.
            escalate.append(f)

        elif f.check == "quant inbox" and sync_quant is not None:
            # MECHANICAL. The transport is a deduped file copy, idempotent on
            # (statement, measured_on) -- running it out of band appends only
            # what is new and is a no-op otherwise. It cannot fix quant's side
            # having produced nothing, and the attempt cap escalates that.
            remedies.append(Remedy(
                f.check, "run the quant findings transport",
                "an idempotent deduped copy; a no-op when nothing is new, and "
                "powerless if quant's own export never ran",
                sync_quant))

        elif f.check in ("capture", "signal rate", "dominant gate", "survivors"):
            # NEVER MECHANICAL, and this is the important refusal in this file.
            #
            # "Capture is 15%" is answered by changing how positions are managed.
            # "The signal rate halved" is answered by understanding WHY before
            # touching anything. Both are judgement, and a process that adjusts
            # its own thresholds toward a rate it likes is not self-healing --
            # it is a desk optimising its own scorecard, which is how a gate
            # gets loosened until it stops protecting anything.
            #
            # Timidity is a defect here and so is acting on four trades. These
            # escalate with the numbers attached and a human decides.
            escalate.append(f)

        elif f.check == "flows" and refresh_flows is not None:
            remedies.append(Remedy(f.check, "refetch the flows cache",
                                   "a stale public-feed cache refetches cleanly",
                                   refresh_flows))

        else:
            # NOT MECHANICAL. "tp1 is computed and compared to nothing" and
            # "the observer's state is lost on restart" are DESIGN faults: the
            # fix is new code, and a process that writes and deploys its own
            # code into a live trading desk unattended can introduce a losing
            # bug or widen a risk limit. Escalated with the diagnosis attached
            # so the human loop is fast rather than absent.
            escalate.append(f)
    return remedies, escalate


def render(outcomes: Sequence[Outcome], escalations: Sequence) -> str:
    if not outcomes and not escalations:
        return ""
    lines = [f"SELF-HEAL ({REMEDIATE_VERSION})"]
    for o in outcomes:
        lines.append(f"  {'FIXED ' if o.taken else 'no-op '} {o.fault:<16} "
                     f"{o.action} — {o.detail}")
    if escalations:
        lines.append("")
        lines.append("  NEEDS A HUMAN — these are DESIGN faults, not operational")
        lines.append("  ones. The fix is new code, and code that writes and")
        lines.append("  deploys itself into a live trading desk unattended can")
        lines.append("  introduce a losing bug or widen a risk limit. So they are")
        lines.append("  reported immediately rather than fixed quietly:")
        for f in escalations:
            lines.append(f"    * {f.check}: {f.detail}")
    return "\n".join(lines)

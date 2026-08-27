"""Is this desk WIRED, or merely running?

WHY A SECOND KIND OF PREFLIGHT

run_desk.py's preflight checks the world: is MT5 up, does the broker match, does
Telegram deliver. Every one of those passed all day on 2026-08-27 while the desk
was quietly broken in five separate places. They were not world problems:

  cohorts never reached the live desk, so every mechanism priced off the
  cold-start prior forever, no matter how many trades resolved

  tp1 was computed, journalled and rendered, and compared to price by nothing,
  so a +1.88R move banked +0.29R

  the observer's tick count and excursion path reset on every restart, so an
  exit reported "MFE +0.00R, 0 observations" on a trade held for hours

  the learning cycle had no Windows launcher at all -- its only trigger in the
  repo was a Linux systemd unit

  a bar the analyst never answered on left NO ledger row, so a blind session and
  a disciplined one were the same file

Each part worked in isolation and passed its own tests. What was missing was the
JOIN, and a join is invisible to any check that looks at one side of it.

WHAT THIS DOES INSTEAD

Every check below reads RUNTIME STATE and asserts a relationship between two
things that must agree. It runs at boot, costs milliseconds, and reports rather
than blocks -- a desk that refuses to start because an audit is unhappy is worse
than one that starts and says so loudly.

WHAT IT CANNOT DO, stated because the operator asked exactly this: it cannot FIX
a defect. "The compiler computes a field nothing reads" is a design fault that
needs someone to read the code and understand the intent. What a check can do is
make the fault ANNOUNCE ITSELF the same day instead of surviving weeks because
every part looks fine on its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

SELF_AUDIT_VERSION = "audit-2026-08-28-a"

#: Resolved trades before "cohorts is empty" stops being ordinary and starts
#: being a wiring fault. One resolved trade with no cohort is a race; several is
#: a join that does not exist.
COHORT_EXPECTED_AFTER = 1

#: Signals that reached TP1 without a single bank before it is called unwired.
#: Two, not one -- a single trade could legitimately have been rejected by the
#: runner-fraction invariant.
TP1_UNWIRED_AFTER = 2


@dataclass(frozen=True)
class Finding:
    check: str
    ok: bool
    detail: str

    @property
    def line(self) -> str:
        return f"  [{'PASS' if self.ok else 'BROKEN'}] {self.check:<26} {self.detail}"


def _closed(rows: Sequence[dict]) -> list[dict]:
    return [r for r in rows if r.get("kind") == "TRADE_CLOSED"]


def _signals(rows: Sequence[dict]) -> list[dict]:
    return [r for r in rows if r.get("kind") == "SIGNAL"]


def check_cohorts_are_loaded(rows: Sequence[dict], cohorts: Optional[dict]) -> Finding:
    """Resolved history exists AND the desk is holding it.

    THE DEFECT THIS CATCHES. build_cohorts() was correct and called by adapt.py
    and acceptance.py -- never by build_service. LiveDesk.cohorts stayed None
    forever, so ev_gate took its cold-start branch on every decision and a
    mechanism with eighty wins priced exactly like one never traded. Both sides
    looked healthy; only their relationship was wrong.
    """
    n = len(_closed(rows))
    if n < COHORT_EXPECTED_AFTER:
        return Finding("cohorts", True,
                       f"{n} resolved trade(s) — too few to expect cohorts yet")
    if not cohorts:
        return Finding("cohorts", False,
                       f"{n} resolved trades in the ledger and the desk holds NO "
                       f"cohorts. Every mechanism is pricing off the cold-start "
                       f"prior — the desk cannot learn from its own results.")
    return Finding("cohorts", True,
                   f"{len(cohorts)} mechanism(s) loaded from {n} resolved trades")


def check_tp1_is_acted_on(rows: Sequence[dict]) -> Finding:
    """Trades that reached TP1 must show banks.

    THE DEFECT THIS CATCHES. tp1 was computed under the comment "partial bank",
    journalled, rendered to Telegram -- and compared to price by nothing. A
    short reached +1.88R with TP1 at +1.78R and kept +0.29R.
    """
    reached, banked = 0, 0
    for c in _closed(rows):
        mfe, rr1 = c.get("mfe_r"), None
        for s in _signals(rows):
            if str(s.get("t0")) == str(c.get("entry_t0")):
                rr1 = (s.get("decision") or {}).get("rr_tp1")
                break
        if mfe is None or rr1 is None:
            continue
        if float(mfe) >= float(rr1):
            reached += 1
            if any(m.get("source") == "tp1" for m in (c.get("management") or [])):
                banked += 1
    if reached < TP1_UNWIRED_AFTER:
        return Finding("tp1 banking", True,
                       f"{reached} trade(s) reached TP1 — too few to judge")
    if banked == 0:
        return Finding("tp1 banking", False,
                       f"{reached} trades reached TP1 and NONE banked. TP1 is "
                       f"being computed and shown and acted on by nothing.")
    return Finding("tp1 banking", True, f"{banked} of {reached} TP1 touches banked")


def check_excursion_survives(rows: Sequence[dict]) -> Finding:
    """A closed trade must carry a real excursion record.

    THE DEFECT THIS CATCHES. checkpoint() wrote the observer's tick count and
    rehydrate() never read it back, and the path was never written at all, so a
    restart reset both. Exits reported "MFE +0.00R - MAE +0.00R - 0
    observations" on positions held for hours.
    """
    bad = [c for c in _closed(rows)
           if (c.get("observations") == 0
               and float(c.get("mfe_r") or 0) == 0.0
               and float(c.get("mae_r") or 0) == 0.0)]
    if not _closed(rows):
        return Finding("excursion", True, "no closed trades yet")
    if bad:
        return Finding("excursion", False,
                       f"{len(bad)} of {len(_closed(rows))} closed trades carry "
                       f"ZERO observations and zero excursion. The observer is "
                       f"not being fed, or its state is lost on restart — every "
                       f"stop-placement question is unanswerable from this data.")
    return Finding("excursion", True,
                   f"all {len(_closed(rows))} closed trades carry excursion")


def check_blind_bars_are_journalled(rows: Sequence[dict]) -> Finding:
    """The ledger must be able to say the desk was blind.

    Not a failure when zero BLIND rows exist -- that is the healthy case. This
    reports the count so an outage is visible in the audit rather than only in a
    log, and so "quiet market" is never inferred from silence.
    """
    blind = [r for r in rows if r.get("kind") == "BLIND"]
    if not blind:
        return Finding("blind bars", True, "none recorded")
    return Finding("blind bars", True,
                   f"{len(blind)} bar(s) the analyst never answered on — these "
                   f"are NOT refusals and no gate earns credit for them")


def check_ledger_is_growing(rows: Sequence[dict],
                            now: Optional[datetime] = None,
                            quiet_hours: float = 12.0) -> Finding:
    """A ledger that has stopped growing is a desk that has stopped deciding."""
    now = now or datetime.now(timezone.utc)
    stamps = []
    for r in rows:
        raw = r.get("t0") or r.get("ts")
        if not raw:
            continue
        try:
            stamps.append(datetime.fromisoformat(str(raw)))
        except ValueError:
            continue
    if not stamps:
        return Finding("ledger growth", True, "ledger empty — nothing decided yet")
    newest = max(stamps)
    if newest.tzinfo is None:
        newest = newest.replace(tzinfo=timezone.utc)
    age_h = (now - newest).total_seconds() / 3600.0
    if age_h > quiet_hours:
        return Finding("ledger growth", False,
                       f"newest row is {age_h:.1f}h old. Over a weekend this is "
                       f"normal; inside a trading week it means the desk is not "
                       f"reaching decisions at all.")
    return Finding("ledger growth", True, f"newest row {age_h:.1f}h old")


def audit(rows: Sequence[dict], cohorts: Optional[dict] = None,
          now: Optional[datetime] = None) -> list[Finding]:
    return [
        check_cohorts_are_loaded(rows, cohorts),
        check_tp1_is_acted_on(rows),
        check_excursion_survives(rows),
        check_blind_bars_are_journalled(rows),
        check_ledger_is_growing(rows, now),
    ]


def render(findings: Sequence[Finding]) -> str:
    broken = [f for f in findings if not f.ok]
    head = (f"SELF-AUDIT ({SELF_AUDIT_VERSION}) — "
            + ("all wiring checks pass" if not broken
               else f"{len(broken)} WIRING FAULT(S)"))
    out = [head] + [f.line for f in findings]
    if broken:
        out += ["",
                "  These are JOINS, not components. Every part named above works",
                "  in isolation and passes its own tests; what is broken is the",
                "  relationship between two of them, which no single-sided check",
                "  can see. Fixing one needs someone to read the code — this can",
                "  only make it announce itself the same day."]
    return "\n".join(out)

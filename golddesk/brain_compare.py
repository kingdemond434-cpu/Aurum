"""Two brains are running. Which one is actually worth more?

WHY THIS HAS TO EXIST THE DAY THE SECOND BRAIN DOES. A failover chain that is
never measured is a silent change of system: some fraction of the desk's signals
start coming from a different model, under different inputs, and the record says
"provider: codex_local" on a row nobody ever groups by. Six months later the
question "was the fallback any good" has no answer, and the honest reply — that
it was never measured — is worth less than not having built it.

So the moment a read can come from a second brain, this exists to ask three
questions and to refuse all three until the sample supports them.

  VOLUME       how much of the desk's output came from each brain, and how long
              it spent away from the primary. Countable from day one, and the
              only one of the three that is answerable early.

  EXPECTANCY   realised R per resolved trade, per brain, with an interval.
              UNMEASURED below MIN_PER_BRAIN — and it will say UNMEASURED for a
              long time, because a fallback fires only during outages and
              outages are rare, which is the point of them.

  DIVERGENCE   the fallback's trades are not a random sample of the desk's
              trades. They happen when the primary is DOWN, which correlates
              with time of day, with provider load, and therefore with session
              and volatility. Comparing the two means on that sample is not a
              controlled comparison and this module says so in the report rather
              than letting a number imply otherwise.

THE TRAP IT REFUSES TO WALK INTO. The obvious use of this file is "promote the
fallback if it scores better". That inference is unavailable from this data at
any sample size, because the two brains never saw the same states. Establishing
that a second brain is better needs both of them run on the SAME frozen
snapshots — which the desk can do, its snapshots exist for exactly that — and
this module's job is to say that plainly rather than to produce a ranking that
looks like it settles the question.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Optional, Sequence

BRAIN_COMPARE_VERSION = "brains-2026-08-29-a"

#: Resolved trades per brain before an expectancy figure is shown at all.
#: Matches cohort_stats.MIN_FOR_EXPECTANCY so the two cannot disagree about when
#: a mean becomes a number rather than an anecdote.
MIN_PER_BRAIN = 8

_T95 = {1: 12.71, 2: 4.30, 3: 3.18, 4: 2.78, 5: 2.57, 6: 2.45, 7: 2.36,
        8: 2.31, 9: 2.26, 10: 2.23, 12: 2.18, 15: 2.13, 20: 2.09, 25: 2.06,
        30: 2.04}


def _t95(df: int) -> float:
    if df <= 0:
        return float("nan")
    for k in sorted(_T95):
        if df <= k:
            return _T95[k]
    return 1.96


@dataclass
class Brain:
    provider: str
    model: str = ""
    signals: int = 0
    resolved: int = 0
    total_r: float = 0.0
    rs: list[float] = field(default_factory=list)
    fallback_signals: int = 0

    @property
    def expectancy(self) -> Optional[float]:
        if self.resolved < MIN_PER_BRAIN:
            return None
        return round(statistics.fmean(self.rs), 4)

    @property
    def interval(self) -> Optional[tuple[float, float]]:
        if self.resolved < MIN_PER_BRAIN or len(self.rs) < 2:
            return None
        sd = statistics.stdev(self.rs)
        if sd == 0.0:
            return (round(statistics.fmean(self.rs), 4),) * 2   # type: ignore[return-value]
        h = _t95(len(self.rs) - 1) * sd / (len(self.rs) ** 0.5)
        m = statistics.fmean(self.rs)
        return round(m - h, 4), round(m + h, 4)

    def to_dict(self) -> dict:
        return {"provider": self.provider, "model": self.model,
                "signals": self.signals, "resolved": self.resolved,
                "fallback_signals": self.fallback_signals,
                "expectancy_r": self.expectancy, "interval": self.interval}


@dataclass
class Report:
    brains: list[Brain] = field(default_factory=list)
    degraded_seconds: float = 0.0
    recoveries: int = 0

    def get(self, provider: str) -> Optional[Brain]:
        return next((b for b in self.brains if b.provider == provider), None)

    @property
    def measured(self) -> list[Brain]:
        return [b for b in self.brains if b.expectancy is not None]

    def to_dict(self) -> dict:
        return {"version": BRAIN_COMPARE_VERSION,
                "brains": [b.to_dict() for b in self.brains],
                "degraded_seconds": round(self.degraded_seconds, 1),
                "recoveries": self.recoveries}

    def render(self) -> str:
        if not self.brains:
            return ("BRAINS: no analyst-stamped signals in the ledger — "
                    "UNMEASURED, which is not the same as one brain.")
        lines = [f"BRAINS ({BRAIN_COMPARE_VERSION}) — {len(self.brains)} "
                 f"analyst(s) in the record"]
        for b in sorted(self.brains, key=lambda x: -x.signals):
            exp = ("UNMEASURED" if b.expectancy is None
                   else f"{b.expectancy:+.3f}R")
            iv = "" if b.interval is None else \
                f" [{b.interval[0]:+.2f}, {b.interval[1]:+.2f}]"
            fb = f", {b.fallback_signals} as a FALLBACK" if b.fallback_signals else ""
            lines.append(f"  {b.provider:<14} {b.signals:>4} signal(s){fb}, "
                         f"{b.resolved} resolved -> {exp}{iv}")
        if self.recoveries:
            lines.append(f"  the primary went away and came back "
                         f"{self.recoveries} time(s), for "
                         f"{self.degraded_seconds / 3600:.1f}h in total")
        if len(self.measured) < 2:
            lines.append(f"  COMPARISON UNMEASURED — a brain needs "
                         f"{MIN_PER_BRAIN} resolved trades before its mean is a "
                         f"number rather than an anecdote, and a fallback only "
                         f"fires during outages, so this will read UNMEASURED "
                         f"for a long time. That is the honest state.")
        else:
            lines.append("  NOT A CONTROLLED COMPARISON. The fallback traded "
                         "only while the primary was DOWN, which correlates "
                         "with hour, load, session and volatility. A difference "
                         "here is not evidence that one brain reads gold better "
                         "-- that needs both run on the SAME frozen snapshots, "
                         "which this desk's snapshots exist to support.")
        return "\n".join(lines)


def _decision(row: dict) -> dict:
    d = row.get("decision")
    return d if isinstance(d, dict) else {}


def build(rows: Sequence[dict]) -> Report:
    """Count signals per brain and join the resolved ones to their outcome.

    Reads the provider stamp the desk already writes on every SIGNAL row, and
    the `failover` block the chain adds when a read did not come from the
    primary. A row with no provider stamp is counted under "unstamped" rather
    than assigned to whichever brain is most common -- the desk predates the
    stamp, and back-filling a guess is how a record starts lying about itself.
    """
    rep = Report()
    by_name: dict[str, Brain] = {}
    r_by_t0: dict[str, float] = {}

    for r in rows:
        if r.get("kind") != "TRADE_CLOSED" or r.get("evidence_valid") is False:
            continue
        v = r.get("realised_r")
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            r_by_t0[str(r.get("entry_t0"))] = float(v)

    for r in rows:
        if r.get("kind") != "SIGNAL":
            continue
        d = _decision(r)
        name = str(d.get("provider") or "unstamped")
        b = by_name.get(name)
        if b is None:
            b = by_name[name] = Brain(name, str(d.get("model") or ""))
        b.signals += 1
        fo = d.get("failover") or {}
        if isinstance(fo, dict):
            if fo.get("chain_position"):
                b.fallback_signals += 1
            if fo.get("recovered"):
                rep.recoveries += 1
                secs = fo.get("degraded_seconds")
                if isinstance(secs, (int, float)):
                    rep.degraded_seconds += float(secs)
        got = r_by_t0.get(str(r.get("t0")))
        if got is not None:
            b.resolved += 1
            b.total_r += got
            b.rs.append(got)

    rep.brains = list(by_name.values())
    return rep

"""Multi-branch causal search (Alpha-Jungle concept, adapted).

Instead of one thesis -> one critique, Sol branches into the distinct ways this
market can be read — continuation, trap, event repricing, liquidity sweep,
cross-market decoupling, macro transmission — scores each branch from the
deterministic state, and the DEEP path attacks the strongest branches
explicitly. The branch tree is for REASONING only: it proposes interpretations,
never directions, never votes, never refusal authority.

`active_branches` is pure score ranking over the state; the branch list lives
only to give the model a structured interrogation to falsify.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

BRANCHES = ("continuation", "trap", "event repricing", "liquidity sweep",
            "cross-market decoupling", "macro transmission")


@dataclass(frozen=True)
class BranchScore:
    name: str
    score: int
    reasons: tuple[str, ...] = ()


def active_branches(brief, st=None,
                    minutes_to_event: Optional[float] = None,
                    driver_divergences: int = 0,
                    top: int = 3) -> list[BranchScore]:
    """Score every branch against the measured state; return the top `top`."""
    ctx = getattr(brief, "context", None)
    out: list[BranchScore] = []
    reasons: list[str] = []

    r = 0
    reasons = []
    if ctx is not None:
        if ctx.displacement_state in ("CONFIRMED", "EXCEPTIONAL"):
            r += 2
            reasons.append(f"displacement {ctx.displacement_state}")
        if ctx.trend_health in ("STRONG",):
            r += 1
            reasons.append("strong-trend backdrop")
    out.append(BranchScore("continuation", r, tuple(reasons)))

    r, reasons = 0, []
    sweep_any = False
    if ctx is not None:
        sweep_any = ctx.sweep_state == "CONFIRMED" or ctx.reclaim_state == "CONFIRMED"
        if sweep_any:
            r += 2
            reasons.append("recent sweep/reclaim in play")
        if ctx.volatility_state == "LOW":
            r += 1
            reasons.append("low vol: trips are cheap")
    out.append(BranchScore("trap", r, tuple(reasons)))

    r, reasons = 0, []
    if minutes_to_event is not None and minutes_to_event <= 60:
        r += 2
        reasons.append(f"release in ~{minutes_to_event:.0f}m reprices everything")
    else:
        reasons.append("no imminent macro release")
    out.append(BranchScore("event repricing", r, tuple(reasons)))

    r, reasons = 0, []
    sess = getattr(brief, "session", None)
    if sess in ("ASIA", "ROLLOVER"):
        r += 1
        reasons.append(f"thin {sess} liquidity")
    if ctx is not None and (ctx.volatility_state == "HIGH" or
                            ctx.displacement_state == "EXCEPTIONAL"):
        r += 1
        reasons.append("high-vol wicks can hunt stops")
    out.append(BranchScore("liquidity sweep", r, tuple(reasons)))

    r, reasons = 0, []
    if driver_divergences > 0:
        r += 2
        reasons.append(f"{driver_divergences} active driver divergence(s)")
    else:
        reasons.append("no driver divergence measured")
    out.append(BranchScore("cross-market decoupling", r, tuple(reasons)))

    r, reasons = 0, []
    if driver_divergences > 0 and (minutes_to_event is not None
                                   and minutes_to_event <= 120):
        r += 2
        reasons.append("drivers misbehaving into a macro window")
    elif driver_divergences > 0:
        r += 1
        reasons.append("driver state is doing work")
    out.append(BranchScore("macro transmission", r, tuple(reasons)))

    ranked = sorted(out, key=lambda b: b.score, reverse=True)
    return ranked[:top]


def render_branches(branches: Sequence[BranchScore]) -> str:
    if not branches:
        return ""
    lines = ["CANDIDATE READINGS — attack each, keep what survives"]
    for b in branches:
        lines.append(f"  [{'x' * max(1, min(b.score or 1, 2))}] {b.name}"
                     f"  ({', '.join(b.reasons)})")
    return "\n".join(lines)